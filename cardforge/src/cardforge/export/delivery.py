# ruff: noqa: E501  (HTML strings)
"""Designer delivery (stage 7, user 2026-11-10): the chosen sets, what did not pass, alternatives.

Ported from the stage-9 pilot scripts (`scripts/stage09_alternatives.py`, `stage09_final.py`) into
the core. Written next to the run's report:

- `screened_out.json` / `screened_out.html` — every image that is not in a final set, with its
  paths (`final.png` 430×480, the generator's `raw.png`), variant, position, attempt and reasons;
- `alternatives.md` — for every position of every final variant: the card in the set and the images
  that could replace it;
- `delivery/current/` — after the G3 choice: per variant `png/NN-<card>.png`, `sheet-5x2.png`,
  `framed/`, `cards.json`, and `index.html` with the sets, IP comments and «Не прошли отбор».
  A changed delivery moves the previous one to `delivery/vNN/` (history of versions).

Paths in the files are relative to the run folder. Local files only: never published.
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from cardforge.orchestrator.store import RunStore, now

REASONS_RU = {
    "auto_pass": "авто-пропуск GPT: стиль в зоне уверенности, проверки чистые",
    "judge:not_run": "вне зоны авто-пропуска: решает человек",
    "hard_check:crop:flag": "объект касается верхнего края или мало воздуха сверху",
    "hard_check:safe_zone:flag": "объект заходит под элементы рамки",
    "fix_check:crop:unresolved": "исправление обрезки не подтверждено",
    "max_attempts_per_card": "закончились попытки",
    "max_fix_rounds": "исправление не удалось за отведённые попытки",
    "human accept at G2": "принята человеком",
    "human reject at G2": "отклонена человеком",
}
STATE_RU = {
    "accepted": "принята",
    "rejected": "отклонена",
    "flagged": "ждёт решения человека",
    "needs_fix": "на исправлении",
    "failed": "сбой генератора",
    "rendering": "рисуется",
    "evaluating": "проверяется",
    "spec_ready": "не нарисована",
    "planned": "не нарисована",
}


def reason_ru(reason: str) -> str:
    """One core reason code in plain Russian (unknown codes stay as they are)."""
    for key, text in REASONS_RU.items():
        if reason.startswith(key):
            return text
    if reason.startswith("hard_check:"):
        return f"не прошла обязательную проверку `{reason.split(':')[1]}`"
    if reason.startswith("policy:") or reason.startswith("IP-") or reason.startswith("CN-"):
        return f"сомнение политики: {reason}"
    if reason.startswith("g3_return") or reason.startswith("card_reopen"):
        return f"возвращена человеком: {reason.split(':', 1)[-1].strip()}"
    return reason


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _plans(store: RunStore) -> dict[str, dict]:
    return {v["variant_id"]: v for v in store.read_json("plan/variants.json")}


def complete_variants(store: RunStore) -> list[str]:
    """Variants whose 10 cards are all accepted."""
    cards = store.manifest().cards
    by: dict[str, list[str]] = {}
    for c in cards.values():
        by.setdefault(c.variant_id, []).append(c.state)
    plans = _plans(store)
    return sorted(
        v for v, s in by.items() if v in plans and len(s) == len(plans[v]["cards"]) and all(x == "accepted" for x in s)
    )


def policy_notes(store: RunStore, card_id: str, attempt: int | None = None) -> list[str]:
    """What the policy QA doubted on this attempt (default: the card's last one)."""
    card = store.manifest().cards[card_id]
    n = attempt or card.attempts
    rel = f"cards/{card.variant_id}/{card_id}/attempt_{n:02d}/eval.json"
    if not n or not store.exists(rel):
        return []
    ev = store.read_json(rel)
    return [
        f"{v['rule_id']} ({v['verdict']}): {v['evidence']}"
        for v in ev.get("policy_verdicts") or []
        if v.get("verdict") != "pass"
    ]


def run_rows(store: RunStore, final: list[str] | None) -> tuple[list[dict], list[str], list[str]]:
    man = store.manifest()
    plans = _plans(store)
    complete = complete_variants(store)
    finals = [v for v in (final if final is not None else complete) if v in complete]
    rows = []
    for vid, plan in plans.items():
        for no, spec in enumerate(plan["cards"], 1):
            cid = spec["card_id"]
            c = man.cards.get(cid)
            if c is None:
                continue
            card_dir = store.path(f"cards/{vid}/{cid}")
            attempts = sorted(p for p in card_dir.glob("attempt_*") if (p / "final.png").exists())
            for a in attempts:
                n = int(a.name.split("_")[1])
                ev = _load(a / "eval.json") if (a / "eval.json").exists() else {}
                in_set = c.state == "accepted" and c.accepted_attempt == n
                if in_set and vid in finals:
                    status, why = "в итоговом наборе", [reason_ru(c.reason or "")]
                elif in_set:
                    status = "принята, но вариант не выбран" if vid in complete else "принята, вариант неполный"
                    why = [status]
                elif c.state == "accepted" or n < c.attempts:
                    status = "прежняя попытка"
                    why = [reason_ru(r) for r in ev.get("reasons") or []] or ["заменена следующей попыткой"]
                else:
                    status = STATE_RU.get(c.state, c.state)
                    why = [reason_ru(r) for r in (ev.get("reasons") or (c.reason or "").split("; ")) if r]
                rows.append({
                    "variant": vid, "variant_title": plan["title"], "position": no, "card_id": cid,
                    "attempt": n, "object": spec["object_name"], "category": spec["category"],
                    "png_430x480": str((a / "final.png").relative_to(store.root)),
                    "raw": str((a / "raw.png").relative_to(store.root)) if (a / "raw.png").exists() else None,
                    "card_state": c.state, "status": status, "in_final_set": in_set and vid in finals,
                    "reasons_codes": ev.get("reasons") or [], "reasons": why,
                    "policy_notes": [f"{v['rule_id']} ({v['verdict']}): {v['evidence']}"
                                     for v in ev.get("policy_verdicts") or [] if v.get("verdict") != "pass"],
                })  # fmt: skip
    return rows, finals, complete


CSS = (
    ":root{--bg:#f6f5f2;--fg:#1d1d1f;--muted:#666;--card:#fff;--line:#ddd;--warn:#b45309}"
    "@media (prefers-color-scheme:dark){:root{--bg:#18181b;--fg:#f4f4f5;--muted:#a1a1aa;--card:#232326;--line:#3f3f46;--warn:#f59e0b}}"
    "body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;background:var(--bg);color:var(--fg)}main{max-width:1180px;margin:auto}"
    "img{max-width:100%;border-radius:6px}.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}"
    "figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px}figcaption{font-size:12px;color:var(--muted)}"
    "section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:16px 0}"
    ".warn{color:var(--warn)}code{font-size:12px;word-break:break-all}"
)


def write_screened(store: RunStore, final: list[str] | None) -> dict:
    rows, finals, complete = run_rows(store, final)
    screened = [r for r in rows if not r["in_final_set"]]
    store.write("screened_out.json", {"run": store.run_id, "paths_relative_to": str(store.root),
                                      "final_variants": finals, "complete_variants": complete,
                                      "screened_out": screened})  # fmt: skip
    md = [f"# {store.run_id}: альтернативы для позиций итоговых наборов", "",
          f"Итоговые варианты: {', '.join(finals) or '—'}; полные варианты: {', '.join(complete) or '—'}.",
          "Пути — от папки прогона; `final.png` — картинка 430×480, `raw.png` — исходник генератора.",
          "Все картинки вне итоговых наборов с причинами: `screened_out.json` и галерея `screened_out.html`.", ""]  # fmt: skip
    for vid in finals:
        mine = sorted((r for r in rows if r["variant"] == vid and r["in_final_set"]), key=lambda x: x["position"])
        if not mine:
            continue
        md += [f"## {vid} «{mine[0]['variant_title']}»", ""]
        for r in mine:
            md += [f"### Позиция {r['position']} — {r['object']} (кат. {r['category']})", "",
                   f"- **В наборе:** `{r['png_430x480']}` ({r['reasons'][0] or 'принята'})"]  # fmt: skip
            alts = [x for x in rows if not x["in_final_set"]
                    and (x["card_id"] == r["card_id"] or (x["category"] == r["category"] and x["variant"] != vid))]  # fmt: skip
            alts.sort(key=lambda x: (x["card_id"] != r["card_id"], x["status"] != "принята, но вариант не выбран", x["variant"], x["position"]))
            for x in alts:
                tag = "та же карта, " if x["card_id"] == r["card_id"] else f"{x['variant']} поз. {x['position']}, "
                md.append(f"- Альтернатива: {x['object']} — {tag}попытка {x['attempt']}, "
                          f"{'; '.join(dict.fromkeys([x['status'], *x['reasons']]))} — `{x['png_430x480']}`")  # fmt: skip
            if not alts:
                md.append("- Альтернатив нет.")
            md.append("")
    store.write("alternatives.md", "\n".join(md))
    page = ["<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>Не прошли отбор</title><style>{CSS}</style></head><body><main>",
            f"<h1>{html.escape(store.run_id)} — не прошли отбор ({len(screened)})</h1>",
            f"<p>Итоговые варианты: {', '.join(finals) or '—'}. Альтернативы по позициям — <code>alternatives.md</code>.</p>"]  # fmt: skip
    for vid in sorted({r["variant"] for r in screened}):
        page.append(f"<h2>{vid}</h2><div class=g>")
        for r in (x for x in screened if x["variant"] == vid):
            note = "".join(f"<br><span class=warn>{html.escape(n)}</span>" for n in r["policy_notes"])
            page.append(f"<figure><img loading=lazy src='{html.escape(r['png_430x480'])}' alt=''><figcaption><b>{html.escape(r['card_id'])} · "
                        f"{html.escape(r['object'])}</b> (попытка {r['attempt']})<br>{html.escape(r['status'])}: "
                        f"{html.escape('; '.join(r['reasons']))}{note}<br><code>{html.escape(r['png_430x480'])}</code></figcaption></figure>")  # fmt: skip
        page.append("</div>")
    page.append("</main></body></html>")
    store.write("screened_out.html", "\n".join(page))
    return {"final": finals, "complete": complete, "screened_out": len(screened)}


def _sources(store: RunStore, variants: list[str]) -> list[str]:
    man = store.manifest()
    plans = _plans(store)
    out = []
    for vid in variants:
        for spec in plans[vid]["cards"]:
            c = man.cards[spec["card_id"]]
            out.append(f"cards/{vid}/{spec['card_id']}/attempt_{c.accepted_attempt:02d}/final.png")
    return out


def write_delivery(store: RunStore, variants: list[str], main: str | None = None) -> dict:
    """`delivery/current/` for the chosen complete variants; a changed delivery is versioned."""
    plans = _plans(store)
    man = store.manifest()
    base = store.path("delivery")
    current = base / "current"
    sources = _sources(store, variants)
    previous = _load(current / "delivery.json") if (current / "delivery.json").exists() else None
    moved_to = None
    if previous is not None and (previous.get("sources") != sources or previous.get("variants") != variants):
        n = 1
        while (base / f"v{n:02d}").exists():
            n += 1
        moved_to = base / f"v{n:02d}"
        current.rename(moved_to)
    elif current.exists():
        shutil.rmtree(current)
    current.mkdir(parents=True)
    auto = {c for c, r in man.counters.get("auto_pass", {}).items() if not r.get("returned")}
    unconfirmed = {
        c: r for c, r in man.counters.get("unconfirmed", {}).items()
        if man.cards[c].state == "accepted" and man.cards[c].accepted_attempt == r["attempt_no"]
    }  # fmt: skip
    sections = []
    for vid in variants:
        plan = plans[vid]
        dest = current / vid
        (dest / "png").mkdir(parents=True)
        (dest / "framed").mkdir()
        shutil.copy(store.path(f"sheets/{vid}.png"), dest / "sheet-5x2.png")
        rows = []
        for no, spec in enumerate(plan["cards"], 1):
            cid = spec["card_id"]
            c = man.cards[cid]
            src = store.path(f"cards/{vid}/{cid}/attempt_{c.accepted_attempt:02d}/final.png")
            shutil.copy(src, dest / f"png/{no:02d}-{cid}.png")
            framed = store.path(f"framed/{vid}/{cid}.png")
            if framed.exists():
                shutil.copy(framed, dest / f"framed/{no:02d}-{cid}.png")
            by = "авто-пропуск" if cid in auto else "не подтверждена судьями" if cid in unconfirmed else "человек"
            rows.append({"position": no, "card": cid, "object": spec["object_name"], "category": spec["category"],
                         "accepted_by": by,
                         "unconfirmed_reasons": [reason_ru(r) for r in unconfirmed.get(cid, {}).get("reasons", [])],
                         "attempts": c.attempts, "source": str(src.relative_to(store.root)),
                         "policy_notes": policy_notes(store, cid, c.accepted_attempt)})  # fmt: skip
        role = None if main is None else ("main" if vid == main else "alternative")
        (dest / "cards.json").write_text(json.dumps({"run": store.run_id, "variant": vid, "title": plan["title"],
                                                     **({"role": role} if role else {}),
                                                     "cards": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")  # fmt: skip
        notes = [f"{r['card']} {r['object']}: {n}" for r in rows for n in r["policy_notes"]]
        unconf = [f"{r['card']} {r['object']}: {'; '.join(r['unconfirmed_reasons']) or 'без подтверждения судьи'}"
                  for r in rows if r["accepted_by"] == "не подтверждена судьями"]  # fmt: skip
        head = f"Сет {plan['set_index']}" if plan.get("set_index") else vid
        if main:
            by = (man.counters.get("main_variant") or {}).get("by")
            head = (f"Основной вариант ({'выбор человека' if by == 'human' else 'выбор судей'}) — {vid}"
                    if vid == main else f"Альтернатива {variants.index(vid)} — {vid}")  # fmt: skip
        gold = plan.get("gold_slots", 0)
        sec = [f"<section><h2>{head} «{html.escape(plan['title'])}»</h2><p>{html.escape(plan.get('concept') or '')}</p>",
               *([f"<p>Золотых позиций: {gold} (рисуются отдельно, на листе — жёлтые заглушки).</p>"] if gold else []),
               f"<p>Авто-пропуск: {sum(r['accepted_by'] == 'авто-пропуск' for r in rows)}/10. Папка <code>{vid}/</code>: "
               "<code>png/</code> 430×480, <code>sheet-5x2.png</code>, <code>framed/</code>, <code>cards.json</code>.</p>"]  # fmt: skip
        if unconf:
            sec.append("<p class=warn>Не подтверждены судьями — проверьте: принять, перерисовать с правкой или заменить "
                       "альтернативой (<code>alternatives.md</code>):</p><ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in unconf) + "</ul>")
        if notes:
            sec.append("<p class=warn>Комментарии проверки IP и политик:</p><ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>")
        sec.append(f"<img src='{vid}/sheet-5x2.png' alt=''><details><summary>Превью в рамке</summary><div class=g>")
        sec += [f"<figure><img src='{vid}/framed/{r['position']:02d}-{r['card']}.png' alt=''><figcaption>{r['position']}. "
                f"{html.escape(r['object'])} · {r['accepted_by']}</figcaption></figure>" for r in rows]  # fmt: skip
        sec.append("</div></details></section>")
        sections += sec
    coll = store.read_json("plan/collection.json") if store.exists("plan/collection.json") else None
    intro = []
    if coll:
        themes = coll.get("set_themes") or []
        intro = [f"<h1>Коллекция «{html.escape(coll.get('collection_theme', ''))}»</h1>",
                 "<p>Темы сетов: " + ", ".join(f"{i}. {html.escape(t)}" for i, t in enumerate(themes, 1)) + ".</p>",
                 *([f"<p>Сет 16 (гранд-сет, только золотые): «{html.escape(coll['grand_set_theme'])}» — в этом прогоне не рисуется.</p>"]
                   if coll.get("grand_set_theme") else [])]  # fmt: skip
    page = ["<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>CardForge — наборы</title><style>{CSS}</style></head><body><main>",
            *(intro or [f"<h1>Наборы прогона {html.escape(store.run_id)}</h1>"]), *sections,
            "<section><h2>Не прошли отбор</h2><p>Всё, что не вошло в выбранные наборы — невыбранные варианты, прежние попытки, "
            "отклонённые карты — с путями, причиной, вариантом и позицией: <a href='../../screened_out.html'>галерея</a>, "
            "<code>screened_out.json</code>; чем заменить каждую позицию — <a href='../../alternatives.md'>alternatives.md</a>.</p></section>",
            "</main></body></html>"]  # fmt: skip
    (current / "index.html").write_text("\n".join(page), encoding="utf-8")
    meta = {"run": store.run_id, "variants": variants, "main_variant": main, "sources": sources, "written_at": now(),
            "previous_version": moved_to.name if moved_to else None}  # fmt: skip
    (current / "delivery.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    store.emit("delivery_written", {"variants": variants, "previous_version": meta["previous_version"]})
    return {"delivery": str(current), "index": str(current / "index.html"), "previous_version": meta["previous_version"]}
