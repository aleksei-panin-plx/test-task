"""`report.md` and `report.html` for a run (TA §7.2, §9.6, §12.2, §15.2)."""

from __future__ import annotations

import html
import re
from collections import Counter

from cardforge.contracts.generation import CardEvaluation, SetEvaluation
from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CollectionRequest, SetPlan
from cardforge.contracts.run import Event, RunManifest
from cardforge.policy.report import copyright_section, policies_section


def _money(x: float) -> str:
    return f"${x:.4f}"


def autonomy(manifest: RunManifest, events: list[Event], approved: set[str]) -> dict:
    decisions = [e for e in events if e.type == "human_decision"]
    gates = Counter(e.payload.get("gate") for e in decisions if e.payload.get("gate") != "G3")
    judged = [e for e in decisions if "agree_with_judge" in e.payload]
    cards = [c for c in manifest.cards.values() if c.variant_id in approved]
    accepted = [c for c in cards if c.state == "accepted"]
    first = [c for c in accepted if c.first_attempt_accepted]
    return {
        "interventions": dict(gates),
        "interventions_total": sum(gates.values()),
        "judge_disagreements": sum(not e.payload["agree_with_judge"] for e in judged),
        "judge_compared": len(judged),
        "cards_total": len(cards),
        "accepted": len(accepted),
        "first_attempt_accepted": len(first),
        "first_attempt_rate": len(first) / len(cards) if cards else 0.0,
    }


def render_report(
    *,
    run_id: str,
    request: CollectionRequest,
    manifest: RunManifest,
    packs: list[PolicyPack],
    plans: list[SetPlan],
    approved: set[str],
    plan_verdicts: list[PolicyVerdict],
    evaluations: dict[str, CardEvaluation],
    sets: dict[str, SetEvaluation],
    costs: dict,
    durations: dict,
    events: list[Event],
    framed: dict[str, list[str]],
    limitations: list[str],
    panel: dict | None = None,
    models: list[dict] | None = None,
    deciders: dict | None = None,
) -> str:
    auto = autonomy(manifest, events, approved)
    lines = [
        f"# Прогон {run_id}",
        "",
        f"Бриф: «{request.collection_theme}: {request.set_theme}», пожелания: "
        f"«{request.producer_wishes_raw}». Рынки: {', '.join(request.markets) or '—'}.",
        "",
        f"- Состояние: `{manifest.state}`"
        + (f", причина: `{manifest.status_reason}`" if manifest.status_reason else ""),
        f"- Рендер: `{manifest.render_profile}`; LLM: `{manifest.llm_provider.get('name')}`; "
        f"судья: `{manifest.judge_profile}`",
        f"- Гейты: G1 `{request.hitl.g1}`, G2 `{request.hitl.g2}`, "
        f"`review_policy: {request.hitl.review_policy}`",
        f"- Шаблон промпта: `{manifest.versions.get('prompt_template')}`",
        "",
    ]
    if limitations:
        lines += ["**Ограничения этого прогона:**", ""]
        lines += [f"- {x}" for x in limitations]
        lines.append("")
    lines += ["## Варианты", ""]
    for plan in plans:
        states = Counter(
            manifest.cards[c.card_id].state for c in plan.cards if c.card_id in manifest.cards
        )
        status = (
            "не рендерился (отклонён на G1 или тестовый лимит)"
            if plan.variant_id not in approved
            else (f"принято {states.get('accepted', 0)} из {len(plan.cards)}")
        )
        lines += [
            f"### {plan.variant_id} «{plan.title}» — {status}",
            "",
            plan.concept,
            "",
        ]
        if plan.variant_id in approved:
            lines += [f"![Лист {plan.variant_id}](sheets/{plan.variant_id}.png)", ""]
            if framed.get(plan.variant_id):
                links = ", ".join(
                    f"[{p.rsplit('/', 1)[-1][:-4]}]({p})" for p in framed[plan.variant_id]
                )
                lines += [f"Превью в рамке: {links}", ""]
        lines += [
            "| Карта | Объект | Кат. | Фон | Состояние | Попыток | Причины |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in plan.cards:
            st = manifest.cards.get(c.card_id)
            ev = evaluations.get(c.card_id)
            bg = f"{c.background.motif}/{c.background.color}" if c.background else "—"
            reasons = ", ".join(ev.reasons) if ev and ev.reasons else (st.reason if st else "")
            lines.append(
                f"| {c.card_id} | {c.object_name} | {c.category} | {bg} | "
                f"{st.state if st else '—'} | {st.attempts if st else 0} | "
                f"{(reasons or '—').replace('|', '/')} |"
            )
        if plan.variant_id in sets:
            s = sets[plan.variant_id]
            lines += [
                "",
                f"Проверка сета: **{s.decision}**; категории {s.category_distribution}; "
                f"цветов {s.color_rhythm['distinct']}; масштабы {', '.join(s.scale_spread)}; "
                f"типы {', '.join(s.type_spread)}.",
            ]
            lines += [f"- {n}" for n in s.notes]
        lines.append("")
    qa_verdicts = [v for ev in evaluations.values() for v in ev.policy_verdicts]
    lines.append(
        policies_section(request, packs, plans, plan_verdicts + qa_verdicts).rstrip() + "\n"
    )
    lines.append(copyright_section(packs, plan_verdicts + qa_verdicts, manifest).rstrip() + "\n")
    accepted = auto["accepted"]
    total = costs.get("committed_usd", 0.0)
    lines += [
        "## Стоимость",
        "",
        f"- Всего: {_money(total)} из потолка {_money(costs.get('max_cost_usd', 0.0))}"
        + (f"; незакрытых резервов: {costs['unsettled']}" if costs.get("unsettled") else ""),
        "- По видам: "
        + (
            ", ".join(f"{k} {_money(v)}" for k, v in sorted(costs.get("by_kind", {}).items()))
            or "—"
        ),
        f"- На принятую карту: {_money(total / accepted) if accepted else '—'}",
        f"- Время рендера: {durations['render_s']:.1f} с, LLM: {durations['llm_s']:.1f} с; "
        f"на принятую карту: "
        + (f"{(durations['render_s'] + durations['llm_s']) / accepted:.2f} с" if accepted else "—"),
        "",
        "## Автономность",
        "",
        f"- Вмешательств человека (G1/G2, G3 не считается): {auto['interventions_total']} "
        f"{auto['interventions'] or ''}".rstrip(),
        f"- Несогласий человека с судьёй: {auto['judge_disagreements']} из "
        f"{auto['judge_compared']}",
        f"- Принято с первой попытки: {auto['first_attempt_accepted']} из {auto['cards_total']} "
        f"({auto['first_attempt_rate']:.0%})",
        f"- Принято всего: {accepted} из {auto['cards_total']}",
        f"- Статус прогона: `{manifest.state}`"
        + (f" (`{manifest.status_reason}`)" if manifest.status_reason else ""),
        "",
    ]
    lines += models_section(models or [])
    lines += deciders_section(deciders or {})
    if not (deciders or {}).get("judges"):
        lines += judges_section(panel or {}, costs)
    lines += [
        "## Версии",
        "",
    ]
    for key, value in manifest.versions.items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    """Cohen's κ on accept / not accept; None when chance agreement is total."""
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def judges_section(panel: dict, costs: dict) -> list[str]:
    """«Судьи»: every judge's verdict per card attempt, pairwise agreement, cost per judge.

    Answers come from `<attempt>/judges/<id>.json`; nothing is merged and no judge is
    ranked here. Only the deciding judge's verdict entered the card decision."""
    decider = panel.get("decider")
    members = panel.get("members", [])
    lines = ["## Судьи", "", f"Решает: `{decider}`. Остальные ответы только журналируются."]
    if not members:
        return lines + ["", "Судей в панели нет: все карточки без провала проверок шли на G2.", ""]
    ids = [m.id for m in members]
    lines += [
        "",
        "| Судья | Провайдер | Модель | Профиль | Ответов | Стоимость |",
        "|---|---|---|---|---|---|",
    ]
    records = panel.get("records", [])
    by_kind = costs.get("by_kind", {})
    for m in members:
        n = sum(r["judge_id"] == m.id for r in records)
        lines.append(
            f"| `{m.id}`{' (решает)' if m.id == decider else ''} | {m.provider} | {m.model} | "
            f"{m.profile} | {n} | {_money(by_kind.get(f'judge:{m.id}', 0.0))} |"
        )
    off = [j for j in panel.get("configured", []) if j not in ids]
    if off:
        lines += ["", "Выключены в конфиге после старта: " + ", ".join(f"`{j}`" for j in off)]
    verdicts: dict[tuple[str, int], dict[str, str]] = {}
    for r in records:
        verdicts.setdefault((r["card_id"], r["attempt_no"]), {})[r["judge_id"]] = r["outcome"][
            "decision"
        ]
    if len(ids) > 1:
        lines += ["", "**Согласие пар судей** (решение целиком и κ по «принять / не принять»):", ""]
        lines += ["| Пара | Общих | Совпало | κ |", "|---|---|---|---|"]
        for i, x in enumerate(ids):
            for y in ids[i + 1 :]:
                both = [(v[x], v[y]) for v in verdicts.values() if x in v and y in v]
                same = sum(p == q for p, q in both)
                kappa = cohen_kappa(
                    [p == "accept" for p, _ in both], [q == "accept" for _, q in both]
                )
                share = f"{same} ({same / len(both):.0%})" if both else "—"
                lines.append(
                    f"| `{x}` / `{y}` | {len(both)} | {share} | "
                    f"{'—' if kappa is None else f'{kappa:.2f}'} |"
                )
    lines += ["", "**Вердикты по карточкам:**", ""]
    lines += ["| Карта | Попытка | " + " | ".join(f"`{j}`" for j in ids) + " |"]
    lines += ["|---|---|" + "---|" * len(ids)]
    for (card, attempt), row in sorted(verdicts.items()):
        lines.append(f"| {card} | {attempt} | " + " | ".join(row.get(j, "—") for j in ids) + " |")
    return lines + [""]


def models_section(rows: list[dict]) -> list[str]:
    """«Модели»: one row per render profile (each set variant has exactly one)."""
    if len(rows) < 1:
        return []
    lines = [
        "## Модели",
        "",
        "Каждый вариант набора рисует ровно одна модель (ТА §7.3); подмены нет.",
        "",
        "| Профиль | Модель | Варианты | Принято | С первой попытки | Картинок | "
        "Время на картинку | Стоимость |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        per = f"{r['render_s'] / r['images']:.1f} с" if r["images"] else "—"
        lines.append(
            f"| `{r['profile']}` | {r['model']} | {', '.join(r['variants'])} | "
            f"{r['accepted']} из {r['cards']} | {r['first']} | {r['images']} | {per} | "
            f"{_money(r['cost'])} |"
        )
    return lines + [""]


def deciders_section(data: dict) -> list[str]:
    """Candidate mode: the two deciding judges side by side, plus the named rule."""
    if not data.get("judges"):
        return []
    lines = [
        "## Решающие судьи",
        "",
        f"Правило: `{data['rule']}` (задаётся в `config/runtime.toml [decision]`). "
        f"Кандидатов на позицию: {data['candidates']}.",
        "",
        "| Судья | Вид | Провайдер | Модель | Профиль | Кандидатов | Принял кандидатов | "
        "Позиций с принятым | Стоимость | Время |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for j in data["judges"]:
        lines.append(
            f"| `{j['id']}` | {j['kind']} | {j['provider']} | {j['model']} | {j['profile']} | "
            f"{j['items']} | {j['accepted']} | {j['positions_accepted']} из {j['positions']} | "
            f"{_money(j['cost'])} | {j['elapsed_s']:.0f} с |"
        )
    pair = data.get("pair")
    if pair:
        kappa = "—" if pair["kappa"] is None else f"{pair['kappa']:.2f}"
        share = f"{pair['same'] / pair['n']:.0%}" if pair["n"] else "—"
        lines += [
            "",
            f"Согласие по кандидатам («принять / не принять»): совпало {pair['same']} из "
            f"{pair['n']} ({share}), κ {kappa}.",
            f"По позициям: выбор подтверждён {pair['confirmed']}, расхождение "
            f"{len(pair['disagree'])}, никто не принял {pair['neither']}.",
        ]
        if pair["disagree"]:
            lines += ["", "**Расхождения судей** (ушли на G2):", ""]
            lines += [f"- {d}" for d in pair["disagree"]]
    return lines + [""]


_INLINE = [
    (re.compile(r"!\[([^\]]*)\]\(([^)]+)\)"), r'<img alt="\1" src="\2" style="max-width:100%">'),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
]


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def to_html(markdown: str, title: str) -> str:
    """Small converter for the subset this report uses (no extra dependency)."""
    body: list[str] = []
    table: list[list[str]] = []
    in_list = None

    def flush() -> None:
        nonlocal table, in_list
        if table:
            head, *rows = table
            body.append("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr>")
            body.append("</thead><tbody>")
            body.extend("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
            body.append("</tbody></table>")
            table = []
        if in_list:
            body.append(f"</{in_list}>")
            in_list = None

    for line in markdown.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if not all(re.fullmatch(r"-+", c) for c in cells):
                table.append([_inline(c.replace("\\|", "|")) for c in cells])
            continue
        item = re.match(r"^\s*(-|\d+\.)\s+(.*)$", line)
        if item:
            kind = "ul" if item.group(1) == "-" else "ol"
            if table or in_list != kind:
                flush()
                body.append(f"<{kind}>")
                in_list = kind
            body.append(f"<li>{_inline(item.group(2))}</li>")
            continue
        flush()
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            n = len(heading.group(1))
            body.append(f"<h{n}>{_inline(heading.group(2))}</h{n}>")
        elif line.strip():
            body.append(f"<p>{_inline(line)}</p>")
    flush()
    style = (
        "body{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px}"
        "table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #ccc;padding:3px 6px}"
    )
    return (
        f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}'
        f"</title><style>{style}</style></head><body>\n" + "\n".join(body) + "\n</body></html>\n"
    )
