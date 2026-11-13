# ruff: noqa: E501
"""Card fields of Light: `main_object` (form, proportions, main colour) and `environment`
(category 4 only), validators of the set grammar."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from cardforge.contracts.request import CardDraft, CollectionRequest, PlanDraft
from cardforge.llm.fake import fake_plan
from cardforge.planner.validators import load_grammar, load_vocabulary, validate_plans

from .conftest import ROOT

CARD = {
    "object_name": "Чайник",
    "main_object": "пузатый чайник, низкий и широкий, красный с кремовой крышкой",
    "theme_rationale": "чаепитие",
    "category": 1,
    "scale_class": "handheld",
    "object_type": "tool",
    "composition": "3/4",
    "background": {"motif": "rays", "color": "green"},
}


def test_main_object_is_required():
    data = {k: v for k, v in CARD.items() if k != "main_object"}
    with pytest.raises(ValidationError, match="main_object"):
        CardDraft.model_validate(data)


@pytest.mark.parametrize("empty", [None, "", "—", "-", "none", "нет"])
def test_environment_empty_for_categories_1_to_3(empty):
    card = CardDraft.model_validate({**CARD, "environment": empty})
    assert card.environment is None


def test_environment_only_for_category_4():
    with pytest.raises(ValidationError, match="only for category 4"):
        CardDraft.model_validate({**CARD, "environment": "пляж на закате"})
    with pytest.raises(ValidationError, match="category 4 needs an environment"):
        CardDraft.model_validate({**CARD, "category": 4, "background": {"motif": "scene", "color": "blue"}})
    card = CardDraft.model_validate({**CARD, "category": 4, "environment": "причал у воды, вечернее небо",
                                     "background": {"motif": "scene", "color": "blue"}})  # fmt: skip
    assert card.environment == "причал у воды, вечернее небо"


def test_request_counts_drawn_sets():
    assert CollectionRequest(request_id="r", collection_theme="Лето", set_theme="",
                             producer_wishes_raw="", sets_total=2).variants_count == 2  # fmt: skip
    assert CollectionRequest(request_id="r", collection_theme="Лето", set_theme="",
                             producer_wishes_raw="").variants_count == 15  # fmt: skip


def _check(plan: dict) -> dict:
    request = CollectionRequest(request_id="r", collection_theme="Кино", set_theme="",
                                producer_wishes_raw="", sets_total=1)  # fmt: skip
    return validate_plans([PlanDraft.model_validate(plan)], request, load_grammar(ROOT), load_vocabulary(ROOT))


def test_fake_plan_is_valid():
    result = _check(fake_plan(0, "Съёмка"))
    assert result["valid"], result["errors"]
    assert not [w for w in result["warnings"] if "main_object" in w]


def test_category_4_has_no_background_plate():
    plan = fake_plan(0, "Съёмка")
    cat4 = next(c for c in plan["cards"] if c["category"] == 4)
    cat4["background"]["motif"] = "rays"
    assert any("category 4 cannot use motif 'rays'" in e for e in _check(plan)["errors"])


def test_category_1_has_a_plate():
    plan = fake_plan(0, "Съёмка")
    cat1 = next(c for c in plan["cards"] if c["category"] == 1)
    cat1["background"]["motif"] = "scene"
    assert any("category 1 cannot use motif 'scene'" in e for e in _check(plan)["errors"])


def test_main_object_without_colour_warns():
    plan = copy.deepcopy(fake_plan(0, "Съёмка"))
    plan["cards"][0]["main_object"] = "хлопушка, компактная и плоская"
    result = _check(plan)
    assert result["valid"]
    assert any(w.endswith("_c01: main_object names no main colour") for w in result["warnings"])
