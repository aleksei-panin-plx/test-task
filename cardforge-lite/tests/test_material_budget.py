"""Set material budget (config/material_budget.toml, fable-review §2 Q1)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cardforge.contracts.request import CollectionRequest, Hitl, PlanDraft
from cardforge.llm.fake import fake_plan
from cardforge.planner.material_budget import (
    bare_metal_word,
    check_plans,
    load_budget,
    parse_budget,
)

ROOT = Path(__file__).resolve().parents[1]
BUDGET = load_budget(ROOT)


def raw_config() -> dict:
    return tomllib.loads((ROOT / "config/material_budget.toml").read_text(encoding="utf-8"))


BARE = [
    ("silver milk churn with a lid", ["metal"], "silver"),
    ("brass trumpet with a mute, bell facing forward", ["metal"], "brass"),
    ("chrome vintage microphone on a short stand", ["metal"], "chrome"),
    ("round silver steel honey extractor drum with a red crank handle", ["metal"], "silver"),
    ("steel ice axe with a red handle, head up", ["metal"], "steel"),
    ("large ornate black wrought-iron key with a scrolled bow", ["metal"], "wrought-iron"),
    ("black-and-silver retro film camera", ["metal", "glass"], "silver"),
    ("soft stylised chrome bucket full of wet brown clay", ["metal"], "chrome"),
    ("long black blacksmith tongs", ["metal"], "black metal"),
    ("tall grey photo enlarger with a black lamp head", ["metal"], "grey metal"),
    ("gold pocket watch with an open lid", [], "gold"),  # no materials: description only
]

NOT_BARE = [
    ("red metal watering can, spout pointing up", ["metal"]),
    ("green cast-iron hand water pump over a stone trough", ["metal", "stone"]),
    ("white wrought-iron garden bench", ["metal"]),
    ("big red-and-gold circus cannon with its barrel pointing up", ["metal"]),
    ("orange aluminium climbing carabiner", ["metal"]),
    ("carved brown wooden jewellery box with a brass latch", ["wood", "metal"]),
    ("pair of white figure skates with silver blades", ["leather", "metal"]),
    ("large planetarium hall with a big silver star projector", ["metal"]),
    ("tall grey stone clock tower with a copper-green roof", ["stone", "metal"]),
    ("round golden hay bale", ["organic"]),  # golden is a colour here, no metal
    ("black-and-red steam locomotive", ["metal"]),
    ("dark grey stone wall", ["stone"]),  # neutral colour, but not a metal card
    (None, ["metal"]),
]


@pytest.mark.parametrize("text,materials,word", BARE)
def test_bare_metal_main_material(text, materials, word):
    assert bare_metal_word(BUDGET, text, materials) == word


@pytest.mark.parametrize("text,materials", NOT_BARE)
def test_painted_metal_and_small_parts_do_not_count(text, materials):
    assert bare_metal_word(BUDGET, text, materials) is None


def test_limit_is_share_of_set_capped():
    assert BUDGET.limit(10) == 2
    assert BUDGET.limit(5) == 1
    assert BUDGET.limit(20) == 2  # max_cards


def _plan(descriptions: list[str]) -> PlanDraft:
    raw = fake_plan(0, "Вестерн")
    for card, text in zip(raw["cards"], descriptions):
        card["object_description"] = text
        card["materials"] = ["metal"]
    return PlanDraft.model_validate(raw)


METAL = ["brass trumpet", "silver milk churn", "chrome microphone", "steel whisk"]


def test_within_budget_passes():
    result = check_plans(BUDGET, [_plan(METAL[:2] + ["red metal mailbox"])])
    assert not result["errors"] and not result["warnings"]
    assert [h["metal"] for h in result["variants"][0]["cards"]] == ["brass", "silver"]


def test_over_budget_repairs_then_warns_in_last_round():
    plans = [_plan(METAL)]
    first = check_plans(BUDGET, plans)
    assert len(first["errors"]) == 1 and not first["warnings"]
    msg = first["errors"][0]
    assert "4 cards with bare metal" in msg and "budget is 2 of 10" in msg
    assert "_c01 (brass)" in msg and "painted metal" in msg
    last = check_plans(BUDGET, plans, last_round=True)
    assert not last["errors"] and last["warnings"] == [msg]


def test_warn_action_and_disabled():
    data = raw_config() | {"action": "warn"}
    result = check_plans(parse_budget(data), [_plan(METAL)])
    assert not result["errors"] and result["warnings"]
    off = check_plans(parse_budget(raw_config() | {"enabled": False}), [_plan(METAL)])
    assert off == {"variants": [], "errors": [], "warnings": []}


def test_bad_config_is_rejected():
    with pytest.raises(ValueError, match="action"):
        parse_budget(raw_config() | {"action": "fix"})
    with pytest.raises(ValueError, match="max_share"):
        parse_budget(raw_config() | {"max_share": 1.5})


def test_fake_plans_are_within_budget():
    plans = [PlanDraft.model_validate(fake_plan(i, "Вестерн")) for i in range(3)]
    assert not check_plans(BUDGET, plans)["errors"]


def test_plan_task_carries_material_budget():
    from cardforge.planner import planner

    request = CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme="Вестерн",
        producer_wishes_raw="",
        hitl=Hitl(g1="none", g2="none"),
    )

    class Ctx:
        root = ROOT
        packs: list = []

    task = planner.plan_task(Ctx(), request)
    assert task["material_budget"] == {"version": BUDGET.version, "rules": list(BUDGET.hints)}
