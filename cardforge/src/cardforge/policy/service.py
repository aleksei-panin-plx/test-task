"""Policy-aware planning steps in host-agent mode (TA §8.4, §9).

`start` writes the request and the planning task, the host agent answers with
`PlanDraft[]`, `screen` runs both screener passes and either returns repair errors
or freezes `plan/variants.json`, and `report` renders the policy sections.
The full planner, validators and RunStore state machine arrive in stage 4.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CardSpec, CollectionRequest, PlanDraft, SafeZone, SetPlan
from cardforge.orchestrator.store import RunStore
from cardforge.policy import ip_check
from cardforge.policy.loader import resolve_active
from cardforge.policy.report import copyright_section, policies_section
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

PLAN_TASK = "tasks/plan.task.json"
PLAN_ANSWER = "tasks/plan.answer.json"
SCREEN_TASK = "tasks/policy_screen.task.json"
SCREEN_ANSWER = "tasks/policy_screen.answer.json"


def _packs(root: Path, request: CollectionRequest) -> list[PolicyPack]:
    return resolve_active(root, request.markets, request.policy_packs)


def _versions(packs: list[PolicyPack]) -> dict:
    return {
        "policies": [
            {"id": p.policy_id, "version": p.policy_version, "sha256": p.file_sha256} for p in packs
        ]
    }


def _verdict_events(store: RunStore, verdicts: list[PolicyVerdict]) -> None:
    for v in verdicts:
        if v.verdict != "pass":
            store.emit(
                "policy_verdict",
                v.model_dump(),
                card_id=None if v.target == "request" else v.target,
            )


def start(root: Path, run_dir: Path, request: CollectionRequest) -> dict:
    packs = _packs(root, request)
    verdicts = screen_request(request, packs)
    request = request.model_copy(
        update={
            "conflicts": merge_conflicts(
                request.conflicts, conflicts_from(verdicts, packs, request)
            )
        }
    )
    store = RunStore.create(
        run_dir,
        gate_modes={"g1": request.hitl.g1, "g2": request.hitl.g2},
        versions=_versions(packs),
        llm_provider={"name": "host-agent"},
    )
    store.write("request.json", request)
    _verdict_events(store, verdicts)
    plan_rules = [
        {
            "rule_id": r.id,
            "severity": r.severity,
            "title": r.title,
            "semantic": r.match.semantic,
            "alternatives": r.alternatives,
        }
        for _, r in rules_for(packs, "plan")
    ]
    store.write(
        PLAN_TASK,
        {
            "schema_version": "plan.v1",
            "instruction": (
                "Write `variants_count` PlanDraft objects for the request. Policy outranks "
                "producer wishes: no object may match a block rule; prefer alternatives. "
                "Each plan's policy_notes names every conflicting rule id with the replacement "
                "and the reason. Copyright: genre archetypes only, see "
                "policies/ip/baseline.md#Чек-лист авторских прав."
            ),
            "request": request.model_dump(mode="json"),
            "active_packs": [p.policy_id for p in packs],
            "pack_paths": [p.path for p in packs],
            "plan_rules": plan_rules,
            "answer_schema": {"type": "array", "items": PlanDraft.model_json_schema()},
        },
    )
    _set_state(store, "awaiting_agent")
    return {
        "run": str(run_dir),
        "state": "awaiting_agent",
        "next": PLAN_ANSWER,
        "conflicts": [c.model_dump() for c in request.conflicts],
    }


def _set_state(store: RunStore, state: str) -> None:
    manifest = store.manifest()
    if manifest.state != state:
        store.emit("state_changed", {"from": manifest.state, "to": state})
        manifest.state = state
        store.save_manifest(manifest)


def _card_specs(root: Path, request: CollectionRequest, draft: PlanDraft, verdicts) -> SetPlan:
    from cardforge.evaluate.mask_checks import render_safe_zone

    zone = render_safe_zone(root)
    cards = []
    for no, c in enumerate(draft.cards, 1):
        card_id = f"{draft.variant_id}_c{no:02d}"
        seed = int(hashlib.sha256(f"{request.seed}:{card_id}:1".encode()).hexdigest()[:8], 16)
        cards.append(
            CardSpec(
                **c.model_dump(),
                card_id=card_id,
                safe_zone=SafeZone(**{k: zone[k] for k in ("top_pct", "bottom_pct", "side_pct")}),
                style_profile_id="reference_cards_v1",
                policy_flags=[v for v in verdicts if v.target == card_id],
                seed=seed,
            )
        )
    return SetPlan(**draft.model_dump(exclude={"cards"}), cards=cards)


def screen(root: Path, run_dir: Path) -> dict:
    store = RunStore(run_dir)
    request = CollectionRequest.model_validate_json(store.path("request.json").read_text())
    packs = _packs(root, request)
    drafts = [PlanDraft.model_validate(x) for x in store.read_json(PLAN_ANSWER)]
    if len(drafts) != request.variants_count:
        raise ValueError(f"expected {request.variants_count} variants, got {len(drafts)}")
    keyword = screen_request(request, packs) + screen_plan(drafts, request, packs)
    task = semantic_task(request, drafts, packs)
    store.write(SCREEN_TASK, task)
    if not store.path(SCREEN_ANSWER).exists():
        _set_state(store, "awaiting_agent")
        return {"state": "awaiting_agent", "next": SCREEN_ANSWER}
    answer = SemanticAnswer.model_validate(store.read_json(SCREEN_ANSWER))
    if answer.task_sha256 != task["task_sha256"]:
        # A repaired plan needs a fresh semantic pass; an old answer must not clear it.
        _set_state(store, "awaiting_agent")
        return {
            "state": "awaiting_agent",
            "next": SCREEN_ANSWER,
            "reason": "stale answer",
            "task_sha256": task["task_sha256"],
        }
    store.emit(
        "llm_call", {"step": "policy_screen", "provider": answer.provider, "model": answer.model}
    )
    verdicts = merge_semantic(keyword, answer, packs, request)
    request = request.model_copy(
        update={
            "conflicts": merge_conflicts(
                request.conflicts, conflicts_from(verdicts, packs, request)
            )
        }
    )
    store.write("request.json", request)
    errors = plan_errors(verdicts, drafts, request)
    store.write(
        "plan/policy_screen.json",
        {"verdicts": [v.model_dump() for v in verdicts], "errors": errors},
    )
    _verdict_events(store, [v for v in verdicts if v.target != "request"])
    if errors:
        _set_state(store, "planning_repair")
        return {"state": "planning_repair", "errors": errors}
    plans = [_card_specs(root, request, d, verdicts) for d in drafts]
    store.write("plan/variants.json", plans)
    _set_state(store, "planned")
    ip_check.init_pending(store, [p.variant_id for p in plans])
    report(root, run_dir)
    return {
        "state": "planned",
        "variants": [p.variant_id for p in plans],
        "verdicts": len(verdicts),
        "report": str(run_dir / "report.md"),
    }


def report(root: Path, run_dir: Path) -> Path:
    store = RunStore(run_dir)
    request = CollectionRequest.model_validate_json(store.path("request.json").read_text())
    packs = _packs(root, request)
    plans = [SetPlan.model_validate(x) for x in store.read_json("plan/variants.json")]
    verdicts = [
        PolicyVerdict.model_validate(v)
        for v in store.read_json("plan/policy_screen.json")["verdicts"]
    ]
    manifest = store.manifest()
    lines = [
        f"# Прогон {store.run_id}",
        "",
        f"Бриф: «{request.collection_theme}: {request.set_theme}», пожелания: "
        f"«{request.producer_wishes_raw}». Рынки: {', '.join(request.markets) or '—'}.",
        f"Состояние: `{manifest.state}`. Рендер на этом этапе не выполнялся.",
        "",
        "## План",
        "",
    ]
    for plan in plans:
        lines += [
            f"### {plan.variant_id} «{plan.title}»",
            "",
            plan.concept,
            "",
            "| Карта | Объект | Кат. | Фон / поверхность / окружение | Флаги |",
            "|---|---|---|---|---|",
        ]
        for c in plan.cards:
            setting = (
                (c.background.motif if c.background else "") or c.surface or c.environment or ""
            )
            flags = ", ".join(f"{f.rule_id}:{f.verdict}" for f in c.policy_flags) or "—"
            lines.append(f"| {c.card_id} | {c.object_name} | {c.category} | {setting} | {flags} |")
        lines.append("")
    text = "\n".join(lines) + "\n" + policies_section(request, packs, plans, verdicts) + "\n"
    text += copyright_section(packs, verdicts, manifest)
    return store.write("report.md", text)
