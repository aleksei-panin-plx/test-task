"""Report sections «Политики» and «Авторские права» (TA §9.6, §9.8)."""

from __future__ import annotations

from collections import Counter

from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CollectionRequest, SetPlan
from cardforge.contracts.run import RunManifest
from cardforge.policy.loader import BASELINE, CHECKLIST_HEADING

PENDING_WARNING = "Набор нельзя использовать до подтверждения проверки авторских прав человеком."
DISCLAIMER = (
    "Пакеты политик консервативны и не являются юридической экспертизой. "
    "Полнота автоматических проверок не измерена."
)


def _verdict_rows(verdicts: list[PolicyVerdict]) -> list[str]:
    rows = [
        "| Цель | Стадия | Правило | Severity | Вердикт | Действие | Улика |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in verdicts:
        evidence = v.evidence.replace("|", "\\|")
        rows.append(
            f"| {v.target} | {v.stage} | {v.rule_id} | {v.severity} | {v.verdict} | "
            f"{v.action} | {evidence} |"
        )
    return rows


def policies_section(
    request: CollectionRequest,
    packs: list[PolicyPack],
    plans: list[SetPlan],
    verdicts: list[PolicyVerdict],
) -> str:
    lines = ["## Политики", "", DISCLAIMER, "", "**Активные пакеты:**", ""]
    for p in packs:
        reason = (
            "всегда"
            if p.policy_id == BASELINE
            else (
                f"markets: {p.region}"
                if p.region and p.region in request.markets
                else "policy_packs"
            )
        )
        lines.append(
            f"- `{p.policy_id}` {p.policy_version} ({reason}), sha256 `{p.file_sha256[:12]}`"
        )
    lines += ["", "**Конфликты брифа с политиками** (`request.json`):", ""]
    lines += [f"- `{c.rule_id}` [{c.kind}]: {c.resolution}" for c in request.conflicts] or ["- нет"]
    lines += ["", "**Замены на этапе плана** (`policy_notes`):", ""]
    for plan in plans:
        lines.append(f"- **{plan.variant_id} «{plan.title}»:**")
        lines += [f"  - {n}" for n in plan.policy_notes] or ["  - замен нет"]
    plan_hits = [v for v in verdicts if v.stage == "plan" and v.verdict != "pass"]
    qa_hits = [v for v in verdicts if v.stage == "qa" and v.verdict != "pass"]
    lines += ["", f"**Срабатывания на плане:** {len(plan_hits)}", ""]
    if plan_hits:
        lines += _verdict_rows(plan_hits)
    lines += ["", f"**Срабатывания на QA:** {len(qa_hits)}"]
    if qa_hits:
        lines += ["", *_verdict_rows(qa_hits)]
    waiting = [v for v in verdicts if v.action in ("flag_g1", "flag_g2")]
    lines += ["", "**Ждёт решения человека:**", ""]
    lines += [f"- {v.target}: `{v.rule_id}` ({v.action})" for v in waiting] or ["- нет"]
    counts = Counter(v.action for v in verdicts if v.verdict != "pass")
    if counts:
        lines += ["", "Действия: " + ", ".join(f"{k} × {n}" for k, n in sorted(counts.items()))]
    return "\n".join(lines) + "\n"


def copyright_section(
    packs: list[PolicyPack], verdicts: list[PolicyVerdict], manifest: RunManifest
) -> str:
    baseline = next(p for p in packs if p.policy_id == BASELINE)
    ip_ids = {r.id for r in baseline.rules}
    if manifest.runtime_options.get("designer"):
        # Stage 7 (user 2026-11-10): no checklist for the designer; doubts are G2/G3 comments.
        hits = [v for v in verdicts if v.rule_id in ip_ids and v.verdict != "pass"]
        lines = [
            "## Авторские права",
            "",
            "Сценарий плагина: чек-лист человеку не показывается и не подтверждается (решение "
            "пользователя 2026-11-10); сомнения IP-проверки показаны комментариями на G2/G3.",
            "",
            f"**Срабатывания IP-правил (план и QA):** {len(hits)}",
        ]
        if hits:
            lines += ["", *_verdict_rows(hits)]
        return "\n".join(lines) + "\n"
    lines = [
        "## Авторские права",
        "",
        f"Чек-лист из `policies/ip/baseline.md` (раздел «{CHECKLIST_HEADING[3:]}»), "
        f"версия {baseline.policy_version}:",
        "",
    ]
    lines += [f"{i}. [ ] {item}" for i, item in enumerate(baseline.checklist, 1)]
    hits = [v for v in verdicts if v.rule_id in ip_ids and v.verdict != "pass"]
    lines += ["", f"**Срабатывания IP-правил (план и QA):** {len(hits)}"]
    if hits:
        lines += ["", *_verdict_rows(hits)]
    lines += ["", "**Статус `ip_check` по вариантам:**", ""]
    for variant, check in sorted(manifest.ip_check.items()):
        ref = f", событие `{check.event_id}`" if check.event_id else ""
        lines.append(f"- {variant}: `ip_check: {check.status}`{ref}")
    if any(c.status != "confirmed" for c in manifest.ip_check.values()) or not manifest.ip_check:
        lines += ["", f"**{PENDING_WARNING}**"]
    return "\n".join(lines) + "\n"
