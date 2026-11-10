"""Local HTML page for the human G2 review of a run (user request 2026-11-04).

One row per set variant (in comparison mode: per model), one column per card position.
Each image gets Принять / Отклонить / Правка and a comment; the draft lives in the
browser, the result is downloaded as JSON and applied with
`cardforge review <run> --gate g2 --from-file <json>`. The page stays a local file next to
the run: the images are derived from customer references and are never published.

Blind by default (phase 1 of the judge autonomy strategy, 2026-11-04): the page shows no
model, render profile, run, variant or card id, no attempt number and no judge verdict;
rows are shuffled and named «Ряд N», cards are `card-NN`. The mapping back to card ids is
`review/g2-blind.private.json` (operator side). Each card also gets the reason-code
checkboxes of the blind package (codes of `prompts/render/fixes.toml`); a fix may be given
by codes alone. `--open` writes the old page with model and ids for the operator.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import random
from pathlib import Path

from PIL import Image

from cardforge.orchestrator.store import RunStore
from cardforge.prompting.fixes import load_library

PAGE = "review/g2-review.html"
BLIND_MAP = "review/g2-blind.private.json"
EXPORT_FORMAT = "cardforge-g2-review-v2"


def page_id(run_id: str, round_no: int) -> str:
    return hashlib.sha256(f"g2-blind:{run_id}:{round_no}".encode()).hexdigest()[:12]


def _visible_reason(reason: str) -> bool:
    """Blind page: image checks only, no judge or rule verdicts that would bias the label."""
    return reason.startswith(("hard_check:", "policy:", "fix_check:"))


def _embedded(path: Path) -> str:
    """JPEG data URI at card size, so the page works from any folder or browser."""
    with Image.open(path) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def page_data(store: RunStore, *, blind: bool = True, root: Path | None = None) -> dict:
    gate = store.read_json("review/g2.json")
    manifest = store.manifest()
    plans = {p["variant_id"]: p for p in store.read_json("plan/variants.json")}
    items = {i["card_id"]: i for i in gate["items"]}
    variants = manifest.compare_variants or sorted({i["variant_id"] for i in gate["items"]})
    rows = []
    for vid in variants:
        cells = []
        for card in plans[vid]["cards"]:
            cid = card["card_id"]
            state = manifest.cards[cid]
            item = items.get(cid)
            rel = f"cards/{vid}/{cid}/attempt_{state.attempts:02d}/final.png"
            image = item["image"] if item and item.get("image") else rel
            cells.append(
                {
                    "card_id": cid,
                    "image": _embedded(store.path(image)) if store.exists(image) else None,
                    "decidable": item is not None and cid not in gate["decisions"],
                    "decided": gate["decisions"].get(cid, {}).get("decision"),
                    "state": state.state,
                    "reason": "; ".join(item["reasons"]) if item else (state.reason or ""),
                    "attempts": state.attempts,
                }
            )
        profile = manifest.variant_profiles.get(vid, manifest.render_profile or "")
        rows.append({"variant_id": vid, "profile": profile, "cells": cells})
    base = plans[variants[0]]["cards"]
    columns = [
        {
            "n": n,
            "object": c["object_name"],
            "category": c["category"],
            "background": f"{c['background']['motif']}/{c['background']['color']}"
            if c.get("background")
            else "",
        }
        for n, c in enumerate(base, 1)
    ]
    request = store.read_json("request.json")
    round_no = len(gate.get("history", []))
    library = load_library(root or Path(__file__).resolve().parents[3])
    data = {
        "run_id": store.run_id,
        "blind": False,
        "page_id": None,
        "reasons": library.labels,
        # A new review round after redraws: the browser draft must not reuse old marks.
        "round": round_no,
        "brief": f"{request['collection_theme']}: {request['set_theme']}"
        + (f" — «{request['producer_wishes_raw']}»" if request["producer_wishes_raw"] else ""),
        "columns": columns,
        "rows": rows,
    }
    for row in rows:
        for cell in row["cells"]:
            cell["id"] = cell["card_id"]
    return _blind(store, data) if blind else data


def _blind(store: RunStore, data: dict) -> dict:
    """Strip model, profile, run and ids; keep the mapping in a private file."""
    pid = page_id(store.run_id, data["round"])
    rng = random.Random(pid)
    rows = list(data["rows"])
    rng.shuffle(rows)
    cells = [c for r in rows for c in r["cells"]]
    order = list(range(1, len(cells) + 1))
    rng.shuffle(order)
    mapping = {}
    for cell, n in zip(cells, order, strict=True):
        cell["id"] = f"card-{n:02d}"
        mapping[cell["id"]] = cell.pop("card_id")
        cell["attempts"] = None
        reasons = [r.strip() for r in cell["reason"].split(";") if r.strip()]
        if cell["decidable"]:
            cell["reason"] = "; ".join(r for r in reasons if _visible_reason(r))
        else:
            cell["reason"] = ""
    store.write(BLIND_MAP, {"page_id": pid, "round": data["round"], "items": mapping})
    return {
        **data,
        "run_id": None,
        "blind": True,
        "page_id": pid,
        "rows": [
            {"variant_id": f"Ряд {n}", "profile": f"Ряд {n}", "cells": r["cells"]}
            for n, r in enumerate(rows, 1)
        ],
    }


def from_blind_export(store: RunStore, data: dict) -> dict:
    """Map a blind page export (`card-NN` keys) back to card ids; ValueError if it was
    written for another page."""
    if not store.exists(BLIND_MAP):
        raise ValueError("this run has no blind G2 page")
    mapping = store.read_json(BLIND_MAP)
    if data.get("page_id") != mapping["page_id"]:
        raise ValueError("the file was exported from another G2 page or round")
    items = mapping["items"]

    def card(key: str) -> str:
        if key not in items:
            raise ValueError(f"unknown card {key} on this page")
        return items[key]

    return {
        "decisions": {card(k): v for k, v in (data.get("decisions") or {}).items()},
        "notes": {card(k): v for k, v in (data.get("notes") or {}).items()},
        "reasons": {card(k): v for k, v in (data.get("reasons") or {}).items()},
    }


def write_review_page(store: RunStore, *, blind: bool = True, root: Path | None = None) -> Path:
    data = page_data(store, blind=blind, root=root)
    template = Path(__file__).with_name("review_page.html").read_text(encoding="utf-8")
    title = "Разметка G2" if blind else f"Разметка G2 · {data['run_id']}"
    text = template.replace("__TITLE__", html.escape(title)).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    )
    store.write(PAGE, text)
    return store.path(PAGE)
