# ruff: noqa: E501
"""Planner: normalise the brief, compose the collection plan, validate, screen, repair (TA §8).

Every LLM step goes through `LLMProvider`; a `Pending` answer returns control to the run
loop (`awaiting_agent`). Repair is bounded by `MAX_REPAIR_ROUNDS`; after that the run
fails with `planning_failed` and the list of unmet rules.

CardForge Light: the instruction texts live in `prompts/planner/*.md` (editable without code,
`docs/guides/customize-knowledge.md`); the plan ends at G1 — `finalize` gives card ids and policy
flags, no render references.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CollectionRequest, Conflict, PlanDraft
from cardforge.knowledge.catalog import load_cards
from cardforge.llm.provider import LLMProvider, Pending, seal
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.calls import ask
from cardforge.orchestrator.store import RunStore
from cardforge.planner.collection import layout as collection_layout
from cardforge.planner.material_budget import check_plans, load_budget
from cardforge.planner.materials import load_rules, normalize_plans
from cardforge.planner.validators import (
    load_grammar,
    load_vocabulary,
    validate_collection,
    validate_plans,
)
from cardforge.policy.screener import (
    SemanticAnswer,
    conflicts_from,
    merge_conflicts,
    merge_semantic,
    plan_errors,
    rules_for,
    screen_plan,
    screen_request,
    semantic_task,
)

MAX_REPAIR_ROUNDS = 2
PLAN_SCHEMA = "plan.lite.v1"  # plan.v2 + main_object, environment only for category 4
PROMPTS = "prompts/planner"
STYLE_PROFILE_ID = "reference_cards_v1"
FEW_SHOT_SETS = (1, 7, 13)  # garden, seafaring, road trip: different scales and surfaces


@dataclass
class PlanContext:
    root: Path
    store: RunStore
    ledger: BudgetLedger
    llm: LLMProvider
    packs: list[PolicyPack]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def style_profile(root: Path) -> dict:
    path = root / f"knowledge/style_profile/{STYLE_PROFILE_ID}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def style_digest(root: Path) -> dict:
    profile = style_profile(root)
    feedback = _read_json(root / "knowledge/style_profile/human_feedback_v1.json")
    return {
        "rules": [r["statement"] for r in profile["rules"]],
        "background_motifs": profile["background_motifs"],
        "human_feedback": [r["statement"] for r in feedback["rules"]],
        "generation_guidance": feedback.get("generation_guidance", []),
    }


def few_shot(root: Path) -> list[dict]:
    """Three reference sets as examples; `environment` only where the card is a category-4 scene."""
    cards = load_cards(root)
    fields = ("category", "object_name", "scale_class", "object_type", "surface")
    out = []
    for index in FEW_SHOT_SETS:
        rows = [c for c in cards if c["set_index"] == index and c["category"] != 5]
        out.append(
            {
                "set_theme": rows[0]["set_theme"],
                "cards": [
                    {
                        **{f: c[f] for f in fields},
                        "environment": c["environment"] if c["category"] == 4 else None,
                        "background": c["background"],
                    }
                    for c in rows
                ],
            }
        )
    return out


def prompt(root: Path, name: str) -> str:
    """One instruction text from `prompts/planner/<name>.md`: comments dropped, one paragraph."""
    text = (Path(root) / PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return " ".join(text.split())


def _compact_rules(packs: list[PolicyPack], stage: str) -> list[dict]:
    return [
        {
            "pack": p.policy_id,
            "rule_id": r.id,
            "severity": r.severity,
            "title": r.title,
            "applies_to": r.applies_to,
            "semantic": r.match.semantic,
            "keywords": r.match.keywords,
            "alternatives": r.alternatives,
        }
        for p, r in rules_for(packs, stage)
    ]


def normalize(ctx: PlanContext, request: CollectionRequest) -> CollectionRequest | Pending:
    """TA §8.1: the provider fills constraints/preferences/assumptions; policy wins."""
    task = seal(
        {
            "step": "normalize",
            "schema_version": "normalize.v1",
            "instruction": prompt(ctx.root, "normalize"),
            "brief": {
                "collection_theme": request.collection_theme,
                "set_theme": request.set_theme,
                "plan_mode": request.plan_mode,
                "sets_total": request.sets_total,
                "producer_wishes_raw": request.producer_wishes_raw,
                "markets": request.markets,
            },
            "policies": [
                {"rule_id": r.id, "title": r.title, "severity": r.severity}
                for p in ctx.packs
                for r in p.rules
            ],
            "style_rules": style_digest(ctx.root)["rules"],
            "answer_schema": {
                "constraints": ["str"],
                "preferences": ["str"],
                "assumptions": ["str"],
                "conflicts": [Conflict.model_json_schema()],
            },
        }
    )
    reply = ask(ctx.store, ctx.ledger, ctx.llm, "normalize", task)
    if isinstance(reply, Pending):
        return reply
    data = reply.data
    llm_conflicts = [Conflict.model_validate(c) for c in data.get("conflicts", [])]
    request = request.model_copy(
        update={
            "constraints": list(data.get("constraints", [])),
            "preferences": list(data.get("preferences", [])),
            "assumptions": list(data.get("assumptions", [])),
        }
    )
    verdicts = screen_request(request, ctx.packs)
    conflicts = merge_conflicts(
        merge_conflicts(request.conflicts, conflicts_from(verdicts, ctx.packs, request)),
        llm_conflicts,
    )
    request = request.model_copy(update={"conflicts": conflicts})
    for v in verdicts:
        ctx.store.emit("policy_verdict", v.model_dump())
    return request


def plan_task(ctx: PlanContext, request: CollectionRequest, repair: dict | None = None) -> dict:
    vocabulary = load_vocabulary(ctx.root)
    task = {
        "step": "repair" if repair else "plan",
        "schema_version": PLAN_SCHEMA,
        "instruction": prompt(ctx.root, "collection") + " " + prompt(ctx.root, "card_rules"),
        "request": request.model_dump(mode="json"),
        "collection_layout": collection_layout(ctx.root, request.sets_total),
        "set_grammar": load_grammar(ctx.root),
        "vocabulary": {
            "category": {k: v for k, v in vocabulary["category"].items() if k in ("1", "2", "3", "4")},
            **{k: sorted(vocabulary[k]) for k in ("motif", "color", "materials")},
        },
        "style": style_digest(ctx.root),
        "few_shot": few_shot(ctx.root),
        **_material_language(ctx.root),
        **_material_budget(ctx.root),
        "plan_rules": _compact_rules(ctx.packs, "plan"),
        "answer_schema": {
            "collection": {
                "set_themes": ["str: exactly sets_total distinct set themes, in set order"],
                "grand_set_theme": "str | null: the culminating theme of set 16 (only if in the layout)",
            },
            "variants": {"type": "array", "items": PlanDraft.model_json_schema()},
        },
    }
    if repair:
        task["previous_plan"] = repair["plan"]
        if repair.get("collection") is not None:
            task["previous_collection"] = repair["collection"]
        task["errors"] = repair["errors"]
        task["instruction"] += " " + prompt(ctx.root, "repair")
        if repair.get("replaced"):
            # Light: objects the policy screen sent back; their sets' policy_notes must explain them.
            task["replaced_objects"] = [
                {k: r[k] for k in ("card_id", "rule_id", "severity", "object_before")} for r in repair["replaced"]
            ]
        if repair.get("human_edits"):
            # G1 (user 2026-11-10): the person's plan edits, verbatim, in their own words.
            task["human_edits"] = repair["human_edits"]
            task["instruction"] += " " + prompt(ctx.root, "human_edits")
    return seal(task)


def _material_language(root: Path) -> dict:
    rules = load_rules(root)
    if not rules.enabled or not rules.hints:
        return {}
    return {"material_language": {"version": rules.version, "rules": list(rules.hints)}}


def _material_budget(root: Path) -> dict:
    budget = load_budget(root)
    if not budget.enabled or not budget.hints:
        return {}
    return {"material_budget": {"version": budget.version, "rules": list(budget.hints)}}


def _screen_task(request: CollectionRequest, drafts: list[PlanDraft], packs) -> dict:
    task = semantic_task(request, drafts, packs)
    task.pop("task_sha256")
    return seal({"step": "policy_screen", **task})


def compose(
    ctx: PlanContext, request: CollectionRequest, round_no: int, repair: dict | None
) -> dict | Pending:
    """One planning round: ask, parse, validate, screen. Returns a result dict."""
    key = "plan" if round_no == 0 else f"plan.repair{round_no}"
    reply = ask(ctx.store, ctx.ledger, ctx.llm, key, plan_task(ctx, request, repair))
    if isinstance(reply, Pending):
        return reply
    raw = reply.data.get("variants", [])
    meta = reply.data.get("collection")
    try:
        drafts = [PlanDraft.model_validate(x) for x in raw]
    except ValidationError as exc:
        errors = [f"schema: {e['loc']}: {e['msg']}" for e in exc.errors()]
        return {"drafts": None, "raw": raw, "collection": meta, "errors": errors, "warnings": [],
                "verdicts": []}  # fmt: skip
    materials = normalize_plans(load_rules(ctx.root), drafts)
    drafts = materials["plans"]
    budget = check_plans(load_budget(ctx.root), drafts, last_round=round_no >= MAX_REPAIR_ROUNDS)
    grammar = validate_plans(drafts, request, load_grammar(ctx.root), load_vocabulary(ctx.root))
    coll = validate_collection(meta, drafts, request, collection_layout(ctx.root, request.sets_total))
    grammar = {"errors": grammar["errors"] + coll["errors"],
               "warnings": grammar["warnings"] + coll["warnings"]}  # fmt: skip
    keyword = screen_request(request, ctx.packs) + screen_plan(drafts, request, ctx.packs)
    screen_task = _screen_task(request, drafts, ctx.packs)
    screen = ask(ctx.store, ctx.ledger, ctx.llm, f"policy_screen.r{round_no}", screen_task)
    if isinstance(screen, Pending):
        return screen
    # Journaled once per round, after the screen answer (a Pending screen re-runs this round).
    if materials["changes"]:
        ctx.store.emit("material_rules", {"round": round_no, "changes": materials["changes"]})
    if any(v["cards"] for v in budget["variants"]):
        ctx.store.emit("material_budget", {"round": round_no, "variants": budget["variants"]})
    # The envelope was already checked against this task by the provider layer.
    answer = SemanticAnswer(
        task_sha256=screen_task["task_sha256"],
        provider=screen.provider,
        model=screen.model,
        **screen.data,
    )
    verdicts = merge_semantic(keyword, answer, ctx.packs, request)
    return {
        "drafts": drafts,
        "raw": raw,
        "collection": meta,
        "errors": (
            grammar["errors"]
            + materials["errors"]
            + budget["errors"]
            + plan_errors(verdicts, drafts, request)
            + replacement_errors((repair or {}).get("replaced", []), drafts)
        ),
        "warnings": grammar["warnings"] + materials["warnings"] + budget["warnings"],
        "verdicts": verdicts,
    }


def replacements(rounds: list[dict]) -> list[dict]:
    """Objects the policy screen sent back in earlier rounds: card, rule, the object as planned.
    One entry per (card, rule); `rounds` are `plan/rounds/round_N.json` in order."""
    out: dict[tuple[str, str], dict] = {}
    for no, rnd in enumerate(rounds):
        names = {f"{p.get('variant_id')}_c{i:02d}": c.get("object_name")
                 for p in rnd.get("raw") or [] for i, c in enumerate(p.get("cards") or [], 1)}  # fmt: skip
        for v in rnd.get("verdicts", []):
            if v["target"] == "request" or v["verdict"] == "pass":
                continue
            out.setdefault((v["target"], v["rule_id"]), {
                "card_id": v["target"], "variant_id": v["target"].split("_")[0], "rule_id": v["rule_id"],
                "severity": v["severity"], "round": no, "object_before": names.get(v["target"]),
                "evidence": v["evidence"],
            })  # fmt: skip
    return list(out.values())


def replacement_errors(replaced: list[dict], drafts: list[PlanDraft]) -> list[str]:
    """A replaced object must be explained: the set's policy_notes name the rule id."""
    notes = {d.variant_id: " ".join(d.policy_notes) for d in drafts}
    return [
        f"{r['variant_id']}: policy_notes must name {r['rule_id']} — «{r['object_before']}» "
        f"({r['card_id']}) was replaced; write the rule id, the replacement and the reason"
        for r in replaced
        if r["variant_id"] in notes and r["rule_id"] not in notes[r["variant_id"]]
    ]


def finalize(drafts: list[PlanDraft], verdicts: list[PolicyVerdict]) -> list[dict]:
    """The G1 plan per set: card ids (`vNN_cMM`) and the policy verdicts of each card."""
    plans = []
    for draft in drafts:
        cards = []
        for no, c in enumerate(draft.cards, 1):
            card_id = f"{draft.variant_id}_c{no:02d}"
            cards.append(
                {
                    "card_id": card_id,
                    **c.model_dump(mode="json"),
                    "policy_flags": [
                        v.model_dump(mode="json") for v in verdicts if v.target == card_id
                    ],
                }
            )
        plans.append({**draft.model_dump(mode="json", exclude={"cards"}), "cards": cards})
    return plans
