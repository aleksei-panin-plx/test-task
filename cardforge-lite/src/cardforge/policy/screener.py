"""PolicyScreener: keyword filter, then a semantic check by an LLM provider (TA §9.4).

The semantic pass runs through the `host-agent` task/answer protocol (TA §8.4): the
screener writes the task, the provider answers, and the answer is merged here. Keyword
violations are never overridden by a semantic `pass`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from pydantic import Field

from cardforge.contracts.policy import PolicyPack, PolicyRule, PolicyVerdict, Strict
from cardforge.contracts.request import (
    CardDraft,
    CollectionRequest,
    Conflict,
    PlanDraft,
)
from cardforge.policy.actions import action_for, blocks_plan
from cardforge.policy.matching import find_terms

TASK_SCHEMA_VERSION = "policy_screen.v1"


class SemanticFinding(Strict):
    target: str
    rule_id: str
    verdict: str = Field(pattern="^(violation|uncertain)$")
    evidence: str


class SemanticAnswer(Strict):
    schema_version: str = TASK_SCHEMA_VERSION
    task_sha256: str  # the answer is valid only for the exact task it was written for
    provider: str
    model: str
    findings: list[SemanticFinding] = Field(default_factory=list)


def rules_for(packs: Iterable[PolicyPack], stage: str) -> list[tuple[PolicyPack, PolicyRule]]:
    return [(p, r) for p in packs for r in p.rules if stage in r.stages]


def card_fields(card: CardDraft) -> dict[str, str]:
    """Plan text per policy target; `must_not_have` is excluded because it names what to avoid."""
    background = " ".join(
        x
        for x in (
            card.background.motif if card.background else "",
            card.surface or "",
            card.environment or "",
        )
        if x
    )
    obj = " ".join(
        [
            card.object_name,
            card.main_object,
            card.object_description or "",  # the English text the image model receives
            card.theme_rationale,
            card.composition,
            *card.materials,
            *card.must_have,
        ]
    )
    return {"object": obj, "background": background}


def _keyword_verdicts(
    target: str, fields: dict[str, str], rules, stage: str, request: CollectionRequest
) -> list[PolicyVerdict]:
    out: list[PolicyVerdict] = []
    for _, rule in rules:
        hits = {
            kind: find_terms(text, rule.match.keywords)
            for kind, text in fields.items()
            # The brief is free text: every plan rule applies to it whatever its target.
            if (kind == "brief" or kind in rule.applies_to) and text
        }
        hits = {k: v for k, v in hits.items() if v}
        if not hits:
            continue
        evidence = "; ".join(f"{k}: {', '.join(v)}" for k, v in hits.items())
        out.append(
            PolicyVerdict(
                rule_id=rule.id,
                stage=stage,
                target=target,
                verdict="violation",
                severity=rule.severity,
                evidence=f"keyword {evidence}",
                action=action_for(rule.severity, "violation", stage, request.hitl),
            )
        )
    return out


def request_fields(request: CollectionRequest) -> dict[str, str]:
    wishes = " ".join([request.producer_wishes_raw, *request.constraints, *request.preferences])
    return {"brief": f"{request.collection_theme}: {request.set_theme}. {wishes}"}


def screen_request(request: CollectionRequest, packs: list[PolicyPack]) -> list[PolicyVerdict]:
    rules = rules_for(packs, "plan")
    return _keyword_verdicts("request", request_fields(request), rules, "plan", request)


def conflicts_from(
    verdicts: list[PolicyVerdict], packs: list[PolicyPack], request: CollectionRequest
) -> list[Conflict]:
    """Policy always wins over a wish (TA §8.1); one conflict per triggered rule."""
    by_id = {r.id: (p, r) for p in packs for r in p.rules}
    out: list[Conflict] = []
    for v in verdicts:
        if v.target != "request" or v.verdict == "pass":
            continue
        pack, rule = by_id[v.rule_id]
        verb = {
            "block": "исключено из плана",
            "review": "заменяется консервативно или выносится на G1",
            "advise": "по возможности заменяется",
        }[rule.severity]
        alt = "; ".join(rule.alternatives[:4]) or "жанровая альтернатива"
        out.append(
            Conflict(
                kind=pack.policy_kind,
                wish=request.producer_wishes_raw,
                rule_id=rule.id,
                resolution=(
                    f"{rule.id} ({rule.severity}, {rule.title}) приоритетнее пожелания "
                    f"[{v.evidence}]: {verb}. Альтернативы: {alt}."
                ),
            )
        )
    return out


def merge_conflicts(existing: list[Conflict], new: list[Conflict]) -> list[Conflict]:
    seen = {(c.kind, c.rule_id) for c in existing}
    return existing + [c for c in new if (c.kind, c.rule_id) not in seen]


def screen_plan(
    plans: list[PlanDraft], request: CollectionRequest, packs: list[PolicyPack]
) -> list[PolicyVerdict]:
    rules = rules_for(packs, "plan")
    out: list[PolicyVerdict] = []
    for plan in plans:
        for no, card in enumerate(plan.cards, 1):
            target = f"{plan.variant_id}_c{no:02d}"
            out += _keyword_verdicts(target, card_fields(card), rules, "plan", request)
    return out


def semantic_task(
    request: CollectionRequest, plans: list[PlanDraft], packs: list[PolicyPack]
) -> dict:
    """Task for the semantic pass. Only rule ids, titles and `semantic` go to the provider."""
    task = {
        "schema_version": TASK_SCHEMA_VERSION,
        "instruction": (
            "For every item, report each listed rule whose `semantic` description the item "
            "matches or plausibly matches (verdict `uncertain`). Omit clean pairs. Evidence "
            "quotes the item text. Do not invent rules; policy outranks producer wishes."
        ),
        "rules": [
            {
                "rule_id": r.id,
                "severity": r.severity,
                "title": r.title,
                "semantic": r.match.semantic,
                "applies_to": r.applies_to,
            }
            for _, r in rules_for(packs, "plan")
        ],
        "items": [{"target": "request", **request_fields(request)}]
        + [
            {"target": f"{p.variant_id}_c{no:02d}", **card_fields(c)}
            for p in plans
            for no, c in enumerate(p.cards, 1)
        ],
        "answer_schema": SemanticAnswer.model_json_schema(),
    }
    task["task_sha256"] = hashlib.sha256(
        json.dumps(task, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return task


def merge_semantic(
    keyword: list[PolicyVerdict],
    answer: SemanticAnswer,
    packs: list[PolicyPack],
    request: CollectionRequest,
) -> list[PolicyVerdict]:
    by_id = {r.id: r for p in packs for r in p.rules if "plan" in r.stages}
    seen = {(v.target, v.rule_id) for v in keyword}
    out = list(keyword)
    for f in answer.findings:
        rule = by_id.get(f.rule_id)
        if rule is None:
            raise ValueError(f"semantic answer cites inactive or unknown rule {f.rule_id}")
        if (f.target, f.rule_id) in seen:
            continue
        seen.add((f.target, f.rule_id))
        out.append(
            PolicyVerdict(
                rule_id=rule.id,
                stage="plan",
                target=f.target,
                verdict=f.verdict,
                severity=rule.severity,
                evidence=f"semantic ({answer.provider}/{answer.model}): {f.evidence}",
                action=action_for(rule.severity, f.verdict, "plan", request.hitl),
            )
        )
    return out


def plan_errors(
    verdicts: list[PolicyVerdict], plans: list[PlanDraft], request: CollectionRequest
) -> list[str]:
    """Errors that send the plan back to repair (TA §8.3)."""
    errors = [
        f"{v.target}: {v.rule_id} ({v.severity}) — {v.evidence}"
        for v in verdicts
        if v.target != "request" and blocks_plan(v.action)
    ]
    rule_ids = {c.rule_id for c in request.conflicts if c.rule_id}
    for plan in plans:
        notes = " ".join(plan.policy_notes)
        for rule_id in sorted(rule_ids):
            if rule_id not in notes:
                errors.append(f"{plan.variant_id}: policy_notes must explain {rule_id}")
    return errors
