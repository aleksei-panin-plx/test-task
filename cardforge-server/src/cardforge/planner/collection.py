# ruff: noqa: E501
"""Collection plan layout (user 2026-11-10): a collection is 16 sets × 10 cards on one broad theme.

Sets 1–15 mix object cards and gold cards; gold cards (the collection's key characters in an
ironic emotional situation) are never planned here — only their slots are marked. The number of
gold slots per set follows the reference collection (`knowledge/catalog/set_statistics.json`,
category 5): 0 in sets 1–6, then 1, 1, 2, 2, 2, 2, 3, 3, 4 in sets 7–15; gold cards close the set.
Set 16 is the grand set, gold only: the plan names its culminating theme and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

CARDS_PER_SET = 10
SETS_TOTAL = 16
GRAND_SET = 16


def gold_pattern(root: Path) -> dict[int, int]:
    rows = json.loads((Path(root) / "knowledge/catalog/set_statistics.json").read_text(encoding="utf-8"))
    return {int(r["set_index"]): int(r["categories"].get("5", 0)) for r in rows}


def layout(root: Path, sets_total: int = SETS_TOTAL) -> dict:
    """The sets of a collection plan: the first `sets_total` positions of the collection."""
    if not 1 <= sets_total <= SETS_TOTAL:
        raise ValueError(f"sets_total must be 1–{SETS_TOTAL}")
    gold = gold_pattern(root)
    drawn = [
        {
            "set_index": i,
            "variant_id": f"v{i:02d}",
            "gold_slots": gold.get(i, 0),
            "object_cards": CARDS_PER_SET - gold.get(i, 0),
        }
        for i in range(1, min(sets_total, GRAND_SET - 1) + 1)
    ]
    return {"sets_total": sets_total, "drawn": drawn, "grand_set": sets_total == GRAND_SET}
