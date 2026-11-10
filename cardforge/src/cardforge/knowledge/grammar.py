"""Data-backed set rules with separate historical and new-production modes."""

from __future__ import annotations

from collections import Counter

from cardforge.knowledge.catalog import Card


def check_set(cards: list[Card], grammar: dict, *, mode: str = "reference") -> dict:
    if mode not in {"reference", "production"}:
        raise ValueError("mode must be reference or production")
    errors, warnings, applied = [], [], []
    if len(cards) != 10 or len({c.card_ref for c in cards}) != len(cards):
        errors.append("exactly ten distinct cards required")
    if len({c.set_index for c in cards}) != 1 or len({c.set_theme for c in cards}) != 1:
        errors.append("mixed set identity/theme")
    objects = [c for c in cards if c.category != 5]
    categories = Counter(c.category for c in cards)
    index = str(cards[0].set_index) if cards else ""
    if mode == "reference" and index == "16":
        if categories != {5: 10}:
            errors.append("grand finale must contain ten character cards")
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "applied_exceptions": [],
            "mode": mode,
        }
    if mode == "production" and categories[5]:
        errors.append("production brief excludes gold character cards")
    if mode == "reference" and categories[5] > 4:
        errors.append("ordinary reference set has more than four gold cards")
    missing = set(range(1, 5)) - set(categories)
    allowed = set(grammar["historical_missing_categories"].get(index, {}).get("categories", []))
    if mode == "reference" and missing and missing <= allowed:
        applied.append(f"set_{index}: documented missing categories {sorted(missing)}")
    elif missing:
        errors.append(f"missing categories {sorted(missing)}")
    if len({c.scale_class for c in objects}) < grammar["min_object_scales"]:
        errors.append("insufficient scale diversity")
    if len({c.object_type for c in objects}) < grammar["min_object_types"]:
        errors.append("insufficient type diversity")
    if len({c.background.color for c in objects}) < grammar["min_object_colors"]:
        errors.append("insufficient background color diversity")
    if not {"vehicle", "structure"} & {c.scale_class for c in objects}:
        warnings.append(
            "no vehicle/structure; acceptable historical variation, review semantic range"
        )
    for a, b in zip(objects, objects[1:]):
        if a.background.color == b.background.color:
            warnings.append(f"adjacent equal color: {a.card_ref}/{b.card_ref}")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "applied_exceptions": applied,
        "mode": mode,
    }
