from types import SimpleNamespace

import pytest

from cardforge.evaluate.judge_inputs import select_references, validate_teacher_examples


def card(name, category, *, digest=None, scale="handheld", materials=("metal",)):
    return SimpleNamespace(
        card_ref=name,
        source_sha256=digest or name,
        category=category,
        scale_class=scale,
        object_type="tool",
        surface="wood",
        materials=materials,
    )


def test_roles_exclude_self_duplicates_and_gold_for_object_cards():
    item = {
        "item_id": "self",
        "image_sha256": "same-image",
        "spec": {
            "category": 3,
            "reference_tags": {"scale_class": "vehicle", "materials": ["wood"]},
        },
    }
    cards = [
        card("self", 3),
        card("duplicate", 3, digest="same-image"),
        card("gold", 5),
        card("style", 1),
        card("context", 3, scale="vehicle"),
        card("material", 2, materials=("wood",)),
        card("other-context", 3),
    ]
    result = select_references(item, cards, ["gold", "self", "style"])
    assert [r["card_ref"] for r in result] == ["style", "context", "material"]
    assert [r["role"] for r in result] == ["style", "category", "material"]
    assert result == select_references(item, list(reversed(cards)), ["gold", "self", "style"])


def test_missing_category_reference_is_an_error_not_wrong_category_fallback():
    item = {"item_id": "test", "image_sha256": "test", "spec": {"category": 5}}
    with pytest.raises(ValueError, match="category reference"):
        select_references(item, [card("style", 1), card("other", 2)], ["style"])


def teaching_case():
    item = {"item_id": "train", "image_sha256": "a" * 64, "reference_group": "popsicle"}
    return {
        "item": item,
        "label": {
            "item_id": "train",
            "image_sha256": "a" * 64,
            "cohort": "borderline",
            "accepted": True,
            "source": "human",
            "reviewer": "fixture",
            "note": "Accepted with a mild suggestion; not a live human label.",
        },
    }


def test_teaching_separates_images_and_complete_object_groups():
    example = teaching_case()
    query = {"item_id": "test", "image_sha256": "b" * 64, "reference_group": "camera"}
    validate_teacher_examples([example], [query])
    for mutation, value in (("item_id", "train"), ("image_sha256", "a" * 64)):
        with pytest.raises(ValueError, match="overlaps evaluation"):
            validate_teacher_examples([example], [{**query, mutation: value}])
    with pytest.raises(ValueError, match="object group overlaps"):
        validate_teacher_examples([example], [{**query, "reference_group": "popsicle"}])
    with pytest.raises(ValueError, match="provenance required"):
        validate_teacher_examples([example], [{"item_id": "test", "image_sha256": "b" * 64}])
    with pytest.raises(ValueError, match="duplicate"):
        validate_teacher_examples([example, example], [query])


def test_agent_or_stale_teaching_labels_cannot_be_used():
    query = {"item_id": "test", "image_sha256": "b" * 64, "reference_group": "camera"}
    example = teaching_case()
    example["label"]["image_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="matching human"):
        validate_teacher_examples([example], [query])
    example = teaching_case()
    example["label"]["source"] = "agent"
    with pytest.raises(ValueError, match="human-labelled"):
        validate_teacher_examples([example], [query])


def test_optional_name_tie_break_preserves_old_policy_and_excludes_self():
    item = {
        "item_id": "query",
        "image_sha256": "same-image",
        "spec": {
            "object_name": "  ПАЛАТКА!! ",
            "category": 3,
            "reference_tags": {"materials": ["wood"]},
        },
    }
    cards = [
        card("style", 1),
        card("category", 3, materials=()),
        card("a-other", 2, materials=("wood",)),
        card("z-matching", 2, materials=("wood",)),
        card("duplicate", 2, digest="same-image", materials=("wood",)),
    ]
    for candidate in cards:
        candidate.object_name = (
            "палатка" if candidate.card_ref in ("z-matching", "duplicate") else "иной"
        )
    old = select_references(item, cards, ["style"])
    preferred = select_references(item, cards, ["style"], prefer_object_name=True)
    assert old[-1]["card_ref"] == "a-other"
    assert preferred[-1]["card_ref"] == "z-matching"
    assert preferred == select_references(
        item, list(reversed(cards)), ["style"], prefer_object_name=True
    )
    assert preferred[:2] == old[:2]


def test_name_tie_break_does_not_override_material_match():
    item = {
        "item_id": "query",
        "image_sha256": "query",
        "spec": {
            "object_name": "palace",
            "category": 3,
            "reference_tags": {"materials": ["wood", "metal"]},
        },
    }
    cards = [
        card("style", 1, materials=()),
        card("category", 3, materials=()),
        card("matching-material", 2, materials=("wood", "metal")),
        card("matching-name", 2, materials=("wood",)),
    ]
    for candidate in cards:
        candidate.object_name = "palace" if candidate.card_ref == "matching-name" else "other"
    assert (
        select_references(item, cards, ["style"], prefer_object_name=True)[-1]["card_ref"]
        == "matching-material"
    )
