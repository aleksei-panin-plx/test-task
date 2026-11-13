# ruff: noqa: E501  (Russian table headers)
"""G1 content tables: Markdown for the chat, CSV and JSON files (CardForge Light).

Columns of the full version's G1 (card, object, category, background, policy flags) plus the
card description of Light: `main_object` (form, proportions, main colour) and `environment`
(category 4 only, «—» for 1–3). Gold slots are marked rows; set 16 is a theme-only row.
"""

from __future__ import annotations

import csv
import io

from cardforge.orchestrator.store import RunStore

CATEGORY = {1: "1 · в воздухе", 2: "2 · на простой опоре", 3: "3 · на поверхности", 4: "4 · в окружении"}
COLUMNS = ["№", "Объект", "Основной объект", "Категория", "Окружение", "Фон", "Политики"]
CSV_COLUMNS = [
    "set_index", "set_theme", "card_id", "position", "slot", "object_name", "main_object",
    "category", "environment", "background_motif", "background_color", "surface",
    "object_description", "policy_flags",
]  # fmt: skip
DASH = "—"


def _cell(text: object) -> str:
    return str(text if text not in (None, "") else DASH).replace("|", "/").replace("\n", " ")


def _flags(card: dict) -> list[str]:
    return [f"{f['rule_id']}: {f['verdict']}" for f in card.get("policy_flags", [])]


def rows(store: RunStore) -> dict:
    """The plan as one JSON document (also `tables/plan.json`)."""
    coll = store.read_json("plan/collection.json")
    sets = store.read_json("plan/sets.json")
    request = store.read_json("request.json")
    st = store.read_json("state.json")
    themes = coll.get("set_themes", [])
    by_index = {s["set_index"]: s for s in sets}
    out_sets = []
    for i, theme in enumerate(themes, 1):
        s = by_index.get(i)
        if s is None:  # set 16: the grand set, gold only — theme only
            out_sets.append({"set_index": i, "theme": theme, "grand_set": True, "gold_slots": None, "cards": []})
            continue
        cards = [{**c, "position": n, "slot": "object"} for n, c in enumerate(s["cards"], 1)]
        cards += [{"card_id": f"{s['variant_id']}_c{n:02d}", "position": n, "slot": "gold"}
                  for n in range(len(s["cards"]) + 1, len(s["cards"]) + s.get("gold_slots", 0) + 1)]  # fmt: skip
        out_sets.append({"set_index": i, "theme": theme, "grand_set": False, "variant_id": s["variant_id"],
                         "concept": s["concept"], "gold_slots": s.get("gold_slots", 0),
                         "policy_notes": s.get("policy_notes", []), "cards": cards})  # fmt: skip
    validation = store.read_json("plan/validation.json") if store.exists("plan/validation.json") else {}
    return {
        "schema": "cardforge-lite.g1-tables.v1",
        "run_id": store.run_id,
        "status": "approved" if st.get("approved") else "draft",
        "approved": st.get("approved"),
        "collection": {"theme": coll.get("collection_theme"), "sets": request["sets_total"],
                       "set_themes": themes, "grand_set_theme": coll.get("grand_set_theme"),
                       "object_cards": sum(len(s["cards"]) for s in sets),
                       "gold_slots": sum(s.get("gold_slots", 0) for s in sets)},  # fmt: skip
        "conflicts": request.get("conflicts", []),
        "warnings": validation.get("warnings", []),
        "edits_so_far": [e["text"] for e in st.get("plan_edits", [])],
        "sets": out_sets,
    }


def markdown(doc: dict) -> str:
    c = doc["collection"]
    status = "утверждён" if doc["status"] == "approved" else "черновик G1 (ждёт правок или «ок»)"
    lines = [f"# Коллекция «{c['theme']}» — план G1", "",
             f"Статус: {status}. Сетов: {c['sets']}; предметных карт: {c['object_cards']}; "
             f"золотых позиций: {c['gold_slots']}.", "", "## Темы сетов", ""]  # fmt: skip
    for s in doc["sets"]:
        tail = " — гранд-сет, только золотые карты (только тема)" if s["grand_set"] else ""
        lines.append(f"{s['set_index']}. {s['theme']}{tail}")
    for s in doc["sets"]:
        if s["grand_set"]:
            lines += ["", f"## Сет {s['set_index']}. {s['theme']}", "",
                      "Гранд-сет: только тема, карты не планируются."]  # fmt: skip
            continue
        lines += ["", f"## Сет {s['set_index']}. {s['theme']}", "", s["concept"], "",
                  "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]  # fmt: skip
        for card in s["cards"]:
            if card["slot"] == "gold":
                cells = [card["card_id"], "★ золотая позиция", DASH, "5 · золотая (не планируется)", DASH, DASH, DASH]
            else:
                bg = card.get("background") or {}
                cells = [card["card_id"], card["object_name"], card["main_object"], CATEGORY[card["category"]],
                         card.get("environment"), f"{bg.get('motif', DASH)} / {bg.get('color', DASH)}",
                         "; ".join(_flags(card))]  # fmt: skip
            lines.append("| " + " | ".join(_cell(x) for x in cells) + " |")
        lines += ["", f"Золотых позиций: {s['gold_slots']}."]
        if s["policy_notes"]:
            lines.append("Политики: " + "; ".join(s["policy_notes"]))
    return "\n".join(lines) + "\n"


def to_csv(doc: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for s in doc["sets"]:
        if s["grand_set"]:
            w.writerow({"set_index": s["set_index"], "set_theme": s["theme"], "slot": "grand_set"})
            continue
        for card in s["cards"]:
            row = {"set_index": s["set_index"], "set_theme": s["theme"], "card_id": card["card_id"],
                   "position": card["position"], "slot": card["slot"]}  # fmt: skip
            if card["slot"] == "object":
                bg = card.get("background") or {}
                row.update(object_name=card["object_name"], main_object=card["main_object"],
                           category=card["category"], environment=card.get("environment") or DASH,
                           background_motif=bg.get("motif"), background_color=bg.get("color"),
                           surface=card.get("surface"), object_description=card.get("object_description"),
                           policy_flags="; ".join(_flags(card)))  # fmt: skip
            w.writerow(row)
    return buf.getvalue()


def write_tables(store: RunStore) -> dict:
    doc = rows(store)
    store.write("tables/plan.json", doc)
    store.write("tables/plan.md", markdown(doc))
    # UTF-8 with BOM: Excel opens the Cyrillic text correctly.
    store.write("tables/cards.csv", "﻿" + to_csv(doc))
    return doc
