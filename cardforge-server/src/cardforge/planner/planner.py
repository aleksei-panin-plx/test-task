# ruff: noqa: E501
"""Planner: normalise the brief, compose variants, validate, screen, repair (TA §8).

Every LLM step goes through `LLMProvider`; a `Pending` answer returns control to the run
loop (`awaiting_agent`). Repair is bounded by `MAX_REPAIR_ROUNDS`; after that the run
fails with `planning_failed` and the list of unmet rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import (
    CardSpec,
    CollectionRequest,
    Conflict,
    PlanDraft,
    SafeZone,
    SetPlan,
)
from cardforge.knowledge.catalog import load_cards
from cardforge.knowledge.references import background_guide, select_background, select_references
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
PLAN_SCHEMA = "plan.v2"
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
    cards = load_cards(root, apply_masks=False)
    fields = ("category", "object_name", "scale_class", "object_type", "surface", "environment")
    out = []
    for index in FEW_SHOT_SETS:
        rows = [c for c in cards if c.set_index == index and c.category != 5]
        out.append(
            {
                "set_theme": rows[0].set_theme,
                "cards": [
                    {**{f: getattr(c, f) for f in fields}, "background": c.background.model_dump()}
                    for c in rows
                ],
            }
        )
    return out


def _compact_rules(packs: list[PolicyPack], stage: str) -> list[dict]:
    return [
        {
            "rule_id": r.id,
            "severity": r.severity,
            "title": r.title,
            "semantic": r.match.semantic,
            "alternatives": r.alternatives,
        }
        for _, r in rules_for(packs, stage)
    ]


def normalize(ctx: PlanContext, request: CollectionRequest) -> CollectionRequest | Pending:
    """TA §8.1: the provider fills constraints/preferences/assumptions; policy wins."""
    task = seal(
        {
            "step": "normalize",
            "schema_version": "normalize.v1",
            "instruction": (
                "Split the producer's wishes into hard constraints, soft preferences and your "
                "own assumptions. Keep the raw text unchanged. Report conflicts with the listed "
                "policies or style rules; policy always outranks a wish."
            ),
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


COLLECTION_INSTRUCTION = (
    "Plan the whole collection (user 2026-11-10). A collection is 16 sets of 10 cards on one broad "
    "theme (`request.collection_theme`, e.g. «Лето», «Путешествие», «Кино»); its sets are diverse, "
    "non-repeating sub-themes. Write `collection.set_themes`: exactly `sets_total` distinct set themes "
    "in set order, each a clear sub-theme of the collection; the 16th, when present, is the "
    "culminating theme of the grand set (e.g. «Летняя вечеринка»), and `collection.grand_set_theme` "
    "repeats it. Write `variants`: one PlanDraft per set of `collection_layout.drawn`, with its "
    "`variant_id`, `set_index` and `gold_slots` as given, `title` = that set's theme, and exactly "
    "`object_cards` object cards. Gold cards (the collection's key characters in an ironic emotional "
    "situation) are NOT planned: their `gold_slots` close the set and stay empty here; set 16 has no "
    "cards. Every object fits its set theme within the collection theme; no object repeats in any "
    "other set of the collection. Set rules (categories, scales, types, colours) apply to the object "
    "cards of each set."
)


def plan_task(ctx: PlanContext, request: CollectionRequest, repair: dict | None = None) -> dict:
    task = {
        "step": "repair" if repair else "plan",
        "schema_version": PLAN_SCHEMA,
        "instruction": (
            "Write `variants_count` PlanDraft objects of exactly 10 cards for the request. "
            "The collection theme frames every card and the set theme is its genre or "
            "flavour: for collection «Кино» each object belongs to the world of cinema — "
            "shooting, showing or watching a film — in that genre (a western film set and "
            "its props, crew gear, premiere, cinema), not the genre's real world itself "
            "(user feedback 2026-11-03). "
            "Each card is one object that carries the theme by itself: do not attach props, "
            "stickers or accessories (ropes, cacti, moustaches) just to signal the genre; a "
            "supporting element appears only when it explains how the object is used, at most "
            "one or two per card, never two of the same kind (one spotlight, not two), and the "
            "object never points downward (user G2 2026-11-04). The `object_description` names "
            "the object and only those supporting elements: no genre decorations, pictograms or "
            "accessories added to it. "
            "Use only vocabulary values for motif, colour and materials. Follow set_grammar. "
            "Policy outranks producer wishes: no object may match a block rule; prefer "
            "alternatives; each plan's policy_notes names every conflicting rule id with the "
            "replacement and the reason. Copyright: genre archetypes only, see "
            "policies/ip/baseline.md#Чек-лист авторских прав. Functional parts need a "
            "plausible use or mounting context; vary graphic motifs across the collection. "
            "Keep background motifs bold and readable, light but less saturated than the "
            "object, which carries the bright accent colours (user feedback 2026-11-03). "
            "Pick each card's background colour opposite or complementary to the object's main "
            "colour (a brown, orange or yellow object on a blue, cyan or purple backdrop; a red "
            "object on green or cyan), never in the object's own hue family (user G2 "
            "2026-11-04: «цвет фона и основной цвет объекта были противоположными / "
            "комплиментарными»). Describe each object with its real form and proportions, "
            "and name its main colour in `object_description` (e.g. 'red-and-cream "
            "console'), so the backdrop contrast can be checked. "
            "The 10 cards of a variant tell one visual story in card order: from small, "
            "simple objects to bigger and more complex ones, ending with a culmination card "
            "(user G1 2026-11-04: «визуальную линию одной истории — от простых предметов к "
            "сложным и кульминации»); say the story in `concept`. "
            "Give every card an English `object_description` for the image model and judges "
            "(short, concrete, e.g. 'brass spyglass telescope, extended')."
        ),
        "request": request.model_dump(mode="json"),
        "set_grammar": load_grammar(ctx.root),
        "vocabulary": {
            k: sorted(load_vocabulary(ctx.root)[k]) for k in ("motif", "color", "materials")
        },
        "style": style_digest(ctx.root),
        "few_shot": few_shot(ctx.root),
        **_material_language(ctx.root),
        **_material_budget(ctx.root),
        "plan_rules": _compact_rules(ctx.packs, "plan"),
        "answer_schema": {
            "variants": {"type": "array", "items": PlanDraft.model_json_schema()},
        },
    }
    if request.plan_mode == "collection":
        task["collection_layout"] = collection_layout(ctx.root, request.sets_total)
        task["instruction"] = COLLECTION_INSTRUCTION + " " + task["instruction"].replace(
            "Write `variants_count` PlanDraft objects of exactly 10 cards for the request. ", "", 1
        ).replace("The 10 cards of a variant tell", "The object cards of a set tell", 1)
        task["answer_schema"]["collection"] = {
            "set_themes": ["str: exactly sets_total distinct set themes, in set order"],
            "grand_set_theme": "str | null: the culminating theme of set 16 (only if in the layout)",
        }
    if repair:
        task["previous_plan"] = repair["plan"]
        if repair.get("collection") is not None:
            task["previous_collection"] = repair["collection"]
        task["errors"] = repair["errors"]
        task["instruction"] += " Fix every listed error and keep everything else."
        if repair.get("human_edits"):
            # G1 (user 2026-11-10): the person's plan edits, verbatim, in their own words.
            task["human_edits"] = repair["human_edits"]
            task["instruction"] += (
                " `human_edits` are the producer's own G1 edits of `previous_plan` (newest "
                "last): apply every one exactly, keep all other cards unless an edit asks to "
                "change them, and still satisfy set_grammar and the policy rules."
            )
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
    meta = reply.data.get("collection") if request.plan_mode == "collection" else None
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
    if request.plan_mode == "collection":
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
        ),
        "warnings": grammar["warnings"] + materials["warnings"] + budget["warnings"],
        "verdicts": verdicts,
    }


def freeze(
    root: Path,
    refs_dir: Path,
    request: CollectionRequest,
    drafts: list[PlanDraft],
    verdicts: list[PolicyVerdict],
) -> list[SetPlan]:
    zone = _read_json(root / "knowledge/frames/safe_zone.json")
    safe = SafeZone(**{k: zone[k] for k in ("top_pct", "bottom_pct", "side_pct")})
    catalog = load_cards(root, apply_masks=False)
    style_refs = list(style_profile(root)["style_references"])
    guide = background_guide(root)
    plans = []
    for draft in drafts:
        cards = []
        for no, c in enumerate(draft.cards, 1):
            card_id = f"{draft.variant_id}_c{no:02d}"
            seed = card_seed(request.seed, card_id, 1)
            refs = select_references(catalog, c, style_refs, seed, refs_dir)
            cards.append(
                CardSpec(
                    **c.model_dump(),
                    card_id=card_id,
                    safe_zone=safe,
                    reference_roles={
                        **{
                            role: [r.id for r in refs if r.role == role]
                            for role in ("style", "category", "material")
                        },
                        "background": [b] if (b := select_background(catalog, c, guide)) else [],
                    },
                    style_profile_id=STYLE_PROFILE_ID,
                    policy_flags=[v for v in verdicts if v.target == card_id],
                    seed=seed,
                )
            )
        plans.append(SetPlan(**draft.model_dump(exclude={"cards"}), cards=cards))
    return plans


def card_seed(run_seed: int, card_id: str, attempt_no: int) -> int:
    """TA §7.3: seed = hash(run.seed, card_id, attempt_no)."""
    return int(hashlib.sha256(f"{run_seed}:{card_id}:{attempt_no}".encode()).hexdigest()[:8], 16)
