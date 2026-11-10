"""Deterministic, label-free selection of references for distinct review roles."""

from __future__ import annotations

import re
import unicodedata

from cardforge.evaluate.models import Label


def validate_teacher_examples(examples: list[dict], queries: list[dict]) -> None:
    """Training human labels must be separated by image, identity and object group."""
    if not 1 <= len(examples) <= 8:
        raise ValueError("one to eight explicit human teaching examples required")
    query_ids = {r["item_id"] for r in queries}
    query_hashes = {r["image_sha256"] for r in queries}
    query_groups = {r.get("reference_group") for r in queries}
    if None in query_groups or "" in query_groups:
        raise ValueError("object-group provenance required for every evaluation query")
    seen_ids, seen_hashes = set(), set()
    for example in examples:
        item = example["item"]
        label = Label.model_validate(example["label"])
        if (
            label.source != "human"
            or label.cohort != "borderline"
            or label.item_id != item["item_id"]
            or label.image_sha256 != item["image_sha256"]
        ):
            raise ValueError("teaching requires a genuine matching human borderline label")
        if item["item_id"] in seen_ids or item["image_sha256"] in seen_hashes:
            raise ValueError("duplicate teaching image")
        seen_ids.add(item["item_id"])
        seen_hashes.add(item["image_sha256"])
        if item["item_id"] in query_ids or item["image_sha256"] in query_hashes:
            raise ValueError("teaching image overlaps evaluation queries")
        if not item.get("reference_group") or item["reference_group"] in query_groups:
            raise ValueError("teaching object group overlaps evaluation queries")


def select_references(
    item: dict, cards: list, style_ids: list[str], *, prefer_object_name: bool = False
) -> list[dict]:
    category = item["spec"]["category"]
    tags = item["spec"].get("reference_tags", item["spec"])
    eligible = [
        card
        for card in cards
        if card.card_ref != item["item_id"]
        and card.source_sha256 != item["image_sha256"]
        and (category == 5 or card.category != 5)
    ]
    used = set()
    result = []

    def choose(role, candidates, rank):
        pool = [c for c in candidates if c.card_ref not in used]
        if not pool:
            raise ValueError(f"no distinct {role} reference for category {category}")
        card = min(pool, key=lambda c: (*rank(c), c.card_ref))
        used.add(card.card_ref)
        result.append({"card_ref": card.card_ref, "role": role, "category": card.category})

    style = [c for c in eligible if c.card_ref in style_ids]
    choose("style", style, lambda c: (style_ids.index(c.card_ref),))
    choose(
        "category",
        [c for c in eligible if c.category == category],
        lambda c: (
            c.scale_class != tags.get("scale_class"),
            c.object_type != tags.get("object_type"),
            c.surface != tags.get("surface"),
        ),
    )
    materials = set(tags.get("materials", []))

    def normalized_name(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        return re.sub(r"[\W_]+", " ", value).strip()

    query_name = normalized_name(item["spec"].get("object_name", ""))
    choose(
        "material",
        eligible,
        lambda c: (
            -len(materials & set(c.materials)),
            c.object_type != tags.get("object_type"),
            c.category != category,
            bool(
                prefer_object_name
                and query_name
                and normalized_name(getattr(c, "object_name", "")) != query_name
            ),
        ),
    )
    return result
