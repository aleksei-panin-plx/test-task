# ruff: noqa: E501
"""Deterministic plan validators (TA §8.3) over `set_grammar` and the catalog vocabulary.

Thresholds come from `knowledge/set_grammar/v1.yaml`; where the human-reviewed grammar is
softer than TA §6.3 (adjacent colours, vehicle/structure) the data wins and the finding is
a warning. Errors go back to the planner as a repair list.
"""

from __future__ import annotations

import re
from collections import Counter
from itertools import combinations
from pathlib import Path

import yaml

from cardforge.contracts.request import CollectionRequest, PlanDraft

MAX_JACCARD = 0.3
TARGET_COLORS = 5


def load_grammar(root: Path) -> dict:
    return yaml.safe_load((root / "knowledge/set_grammar/v1.yaml").read_text(encoding="utf-8"))


def load_vocabulary(root: Path) -> dict:
    return yaml.safe_load((root / "knowledge/catalog/vocabulary.yaml").read_text(encoding="utf-8"))


def object_key(name: str) -> str:
    return re.sub(r"[^\w]+", " ", name.lower().replace("ё", "е")).strip()


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def validate_variant(plan: PlanDraft, grammar: dict, vocab: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    v = plan.variant_id
    cards = plan.cards
    n = len(cards)
    # Object-card rules scale with 10 − N object cards (user 2026-11-10; grammar `object_count`):
    # all four categories from `all_categories_from` object cards, else one may be missing (the
    # reference sets 7 and 15 with gold cards miss one category); types/colours ≤ n − 2.
    oc = grammar.get("object_count", {})
    need_all = n >= oc.get("all_categories_from", 10)
    missing = sorted(set(range(1, 5)) - {c.category for c in cards})
    if missing and (need_all or len(missing) > 1):
        errors.append(f"{v}: missing categories {missing}")
    elif missing:
        warnings.append(f"{v}: {n} object cards, missing category {missing} (allowed with gold slots)")
    for no, c in enumerate(cards, 1):
        cid = f"{v}_c{no:02d}"
        if c.background is None:
            errors.append(f"{cid}: background (motif + color) is required for the colour plan")
            continue
        if c.background.motif not in vocab["motif"]:
            errors.append(f"{cid}: motif {c.background.motif!r} not in vocabulary")
        if c.background.color not in vocab["color"]:
            errors.append(f"{cid}: color {c.background.color!r} not in vocabulary")
        for m in c.materials:
            if m not in vocab["materials"]:
                warnings.append(f"{cid}: material {m!r} not in vocabulary; reference match weaker")
    scales = {c.scale_class for c in cards}
    min_scales = min(grammar["min_object_scales"], max(1, n - 3))
    if len(scales) < min_scales:
        errors.append(f"{v}: {len(scales)} scale classes, need ≥ {min_scales}")
    if "handheld" not in scales:
        warnings.append(f"{v}: no handheld object")
    if not {"vehicle", "structure"} & scales:
        warnings.append(f"{v}: no vehicle/structure (grammar: preference, not a rule)")
    types = {c.object_type for c in cards}
    min_types = min(grammar["min_object_types"], max(1, n - 2))
    if len(types) < min_types:
        errors.append(f"{v}: {len(types)} object types, need ≥ {min_types}")
    colors = [c.background.color for c in cards if c.background]
    min_colors = min(grammar["min_object_colors"], max(1, n - 2))
    if len(set(colors)) < min_colors:
        errors.append(f"{v}: {len(set(colors))} background colours, need ≥ {min_colors}")
    elif len(set(colors)) < TARGET_COLORS:
        warnings.append(f"{v}: {len(set(colors))} background colours, aim for {TARGET_COLORS}")
    for no, (a, b) in enumerate(zip(colors, colors[1:]), 1):
        if a == b:
            warnings.append(f"{v}: cards {no} and {no + 1} share background colour {a}")
    if plan.color_plan and plan.color_plan != colors:
        warnings.append(f"{v}: color_plan differs from card backgrounds")
    dupes = [n for n, k in Counter(object_key(c.object_name) for c in cards).items() if k > 1]
    if dupes:
        errors.append(f"{v}: repeated objects {dupes}")
    return errors, warnings


def validate_plans(
    plans: list[PlanDraft], request: CollectionRequest, grammar: dict, vocab: dict
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    noun = "sets" if request.plan_mode == "collection" else "variants"
    if len(plans) != request.variants_count:
        errors.append(f"expected {request.variants_count} {noun}, got {len(plans)}")
    if request.plan_mode == "variants":
        errors += [f"{p.variant_id}: a variant has 10 cards, got {len(p.cards)}"
                   for p in plans if len(p.cards) != 10]  # fmt: skip
    ids = [p.variant_id for p in plans]
    if len(set(ids)) != len(ids):
        errors.append(f"variant ids repeat: {ids}")
    for plan in plans:
        e, w = validate_variant(plan, grammar, vocab)
        errors += e
        warnings += w
        if request.category_counts:
            got = Counter(str(c.category) for c in plan.cards)
            if dict(got) != {k: v for k, v in request.category_counts.items() if v}:
                errors.append(
                    f"{plan.variant_id}: categories {dict(got)} ≠ requested "
                    f"{request.category_counts}"
                )
    for a, b in combinations(plans, 2):
        score = jaccard(
            {object_key(c.object_name) for c in a.cards},
            {object_key(c.object_name) for c in b.cards},
        )
        if score > MAX_JACCARD:
            errors.append(
                f"{a.variant_id}/{b.variant_id}: object Jaccard {score:.2f} > {MAX_JACCARD}"
            )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def validate_collection(
    meta: dict | None, plans: list[PlanDraft], request: CollectionRequest, layout: dict
) -> dict:
    """Collection mode (user 2026-11-10): `sets_total` distinct set themes, one plan per drawn set
    with exactly its object-card count and gold slots, no object repeated across sets."""
    errors: list[str] = []
    warnings: list[str] = []
    meta = meta or {}
    themes = [str(t).strip() for t in meta.get("set_themes") or []]
    if len(themes) != request.sets_total:
        errors.append(f"collection: expected {request.sets_total} set themes, got {len(themes)}")
    dupes = [t for t, k in Counter(t.lower() for t in themes).items() if k > 1]
    if dupes:
        errors.append(f"collection: set themes repeat {dupes}")
    by_id = {p.variant_id: p for p in plans}
    for slot in layout["drawn"]:
        plan = by_id.get(slot["variant_id"])
        if plan is None:
            errors.append(f"collection: no plan for set {slot['set_index']} ({slot['variant_id']})")
            continue
        if plan.set_index != slot["set_index"]:
            errors.append(f"{plan.variant_id}: set_index must be {slot['set_index']}")
        if len(plan.cards) != slot["object_cards"]:
            errors.append(
                f"{plan.variant_id}: set {slot['set_index']} has {slot['object_cards']} object "
                f"cards ({slot['gold_slots']} gold slots), got {len(plan.cards)}"
            )
        if plan.gold_slots != slot["gold_slots"]:
            errors.append(f"{plan.variant_id}: gold_slots must be {slot['gold_slots']}")
        if themes and slot["set_index"] <= len(themes) and plan.title.strip() != themes[slot["set_index"] - 1]:
            errors.append(f"{plan.variant_id}: title must be set theme {slot['set_index']} «{themes[slot['set_index'] - 1]}»")
    seen: dict[str, str] = {}
    for p in plans:
        for c in p.cards:
            key = object_key(c.object_name)
            if key in seen and seen[key] != p.variant_id:
                errors.append(f"{p.variant_id}: object «{c.object_name}» repeats set {seen[key]}")
            seen.setdefault(key, p.variant_id)
    if layout["grand_set"] and not str(meta.get("grand_set_theme") or "").strip():
        errors.append("collection: set 16 (grand set, gold only) needs `grand_set_theme`")
    return {"valid": not errors, "errors": errors, "warnings": warnings}
