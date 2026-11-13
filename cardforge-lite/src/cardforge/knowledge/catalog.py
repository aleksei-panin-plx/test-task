"""Reference catalog for the planner's few-shot (Light: rows only, no images, no masks).

`knowledge/catalog/cards.jsonl` holds the labelled reference cards; the human review overlay
(`human_review.jsonl`) applies the person's corrections, checked against the source hash.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def load_cards(root: Path) -> list[dict]:
    folder = Path(root) / "knowledge/catalog"
    cards = read_jsonl(folder / "cards.jsonl")
    by_id = {c["card_ref"]: c for c in cards}
    if len(by_id) != len(cards):
        raise ValueError("duplicate card_ref")
    review = folder / "human_review.jsonl"
    if review.exists():
        for row in read_jsonl(review):
            card = by_id.get(row["card_ref"])
            if card is None or row["source_sha256"] != card["source_sha256"]:
                raise ValueError(f"human review of an unknown or changed card {row['card_ref']}")
            card.update(row.get("corrections") or {})
    return [by_id[c["card_ref"]] for c in cards]
