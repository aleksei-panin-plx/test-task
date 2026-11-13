"""Planner material language rules (config/material_rules.toml, H4)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cardforge.contracts.request import CollectionRequest, Hitl, PlanDraft
from cardforge.llm.fake import fake_plan
from cardforge.planner.materials import (
    load_rules,
    normalize_plans,
    normalize_text,
    parse_rules,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT)


def raw_config() -> dict:
    return tomllib.loads((ROOT / "config/material_rules.toml").read_text(encoding="utf-8"))


# (description, materials, expected after rewrites; sole-neutral fix off)
CASES = [
    # metal is painted by default
    (
        "red metal watering can, spout pointing up",
        ["metal"],
        "red painted metal watering can, spout pointing up",
    ),
    ("orange aluminium climbing carabiner", ["metal"], "orange painted metal climbing carabiner"),
    # silver / steel / chrome → soft stylised chrome
    (
        "shiny silver spoon fishing lure with a small red bead",
        ["metal"],
        "soft stylised chrome spoon fishing lure with a small red bead",
    ),
    (
        "steel balloon whisk with a red handle",
        ["metal", "plastic"],
        "soft stylised chrome balloon whisk with a red handle",
    ),
    (
        "round silver steel honey extractor drum with a red crank handle",
        ["metal"],
        "round soft stylised chrome honey extractor drum with a red crank handle",
    ),
    (
        "galvanised grey metal bucket full of wet brown clay",
        ["metal", "organic"],
        "soft stylised chrome bucket full of wet brown clay",
    ),
    (
        "blue metal bucket with two silver fish",
        ["metal"],
        "blue painted metal bucket with two silver fish",
    ),
    ("steel blue enamel mug", ["metal"], "steel blue enamel mug"),
    # dark iron is slate blue-grey, not black; coloured iron keeps its colour
    (
        "black iron street lantern glowing yellow",
        ["metal", "glass"],
        "slate blue-grey iron street lantern glowing yellow",
    ),
    (
        "large brown wooden chest with iron straps",
        ["wood", "metal"],
        "large honey-orange wooden chest with slate blue-grey iron straps",
    ),
    ("green cast-iron hand water pump", ["metal"], "green cast-iron hand water pump"),
    (
        "big dark steel anvil on a tree-stump base",
        ["metal"],
        "big slate blue-grey steel anvil on a tree-stump base",
    ),
    # gold / brass: warm yellow-orange, only for metal, never food
    ("brass trumpet with a mute", ["metal"], "warm yellow-orange brass trumpet with a mute"),
    ("round golden hay bale", ["organic"], "round golden hay bale"),
    (
        "painted red circus wagon with red-and-gold wheels",
        ["wood", "metal"],
        "painted red circus wagon with red-and-gold wheels",
    ),
    # wood: honey-orange, no dark species, painted wood keeps its paint
    (
        "tall red-brown mahogany grandfather clock",
        ["wood"],
        "tall honey-orange wooden grandfather clock",
    ),
    (
        "dark brown walnut mantel clock with a cream dial",
        ["wood", "glass"],
        "honey-orange wooden mantel clock with a cream dial",
    ),
    ("small red wooden chicken coop", ["wood"], "small red wooden chicken coop"),
    (
        "small three-legged wooden stool painted red",
        ["wood"],
        "small three-legged wooden stool painted red",
    ),
    ("open red-painted wooden toolbox", ["wood"], "open red-painted wooden toolbox"),
    # stone and leather
    (
        "small cairn of stacked grey stones",
        ["stone"],
        "small cairn of stacked light beige-grey stones",
    ),
    (
        "brown leather suitcase with straps",
        ["leather"],
        "caramel-brown leather suitcase with straps",
    ),
    # untouched
    (
        "terracotta orange clay jug with one round handle",
        ["ceramic"],
        "terracotta orange clay jug with one round handle",
    ),
]


def test_config_loads_and_rule_ids_are_unique():
    assert RULES.enabled and RULES.version == raw_config()["version"]
    ids = [r.id for r in RULES.rewrites]
    assert len(ids) == len(set(ids))
    assert RULES.sole_neutral_action == "repair"
    assert RULES.hints and all(h.isascii() for h in RULES.hints)


@pytest.mark.parametrize(("text", "materials", "expected"), CASES)
def test_rewrites(text, materials, expected):
    assert normalize_text(RULES, text, materials, fix=False).after == expected


@pytest.mark.parametrize(("text", "materials", "expected"), CASES)
def test_rewrites_are_idempotent(text, materials, expected):
    once = normalize_text(RULES, text, materials, fix=False).after
    again = normalize_text(RULES, once, materials, fix=False)
    assert again.after == once and not again.changed


def test_sole_neutral_detection_and_fix():
    out = normalize_text(
        RULES, "chrome vintage microphone on a short stand", ["metal"], "red", fix=False
    )
    assert out.after == "soft stylised chrome vintage microphone on a short stand"
    assert out.sole_neutral
    fixed = normalize_text(RULES, out.before, ["metal"], "red", fix=True)
    accent = raw_config()["sole_neutral"]["accent_for_background"]["red"]
    assert fixed.after == f"{accent} and soft stylised chrome vintage microphone on a short stand"
    assert not fixed.sole_neutral and "sole_neutral_fix" in fixed.rules
    # metal black → slate blue-grey (dark iron) before any accent is added
    tongs = normalize_text(RULES, "black camping pot over a campfire", ["metal"], "blue", fix=True)
    assert tongs.after == "slate blue-grey camping pot over a campfire"
    # black-and-white is not grey-only; a colourless description is not either
    assert not normalize_text(RULES, "black-and-white striped cap", ["fabric"]).sole_neutral
    assert not normalize_text(RULES, "round paintbrush", ["wood"]).sole_neutral


def _plans(description: str) -> list[PlanDraft]:
    raw = fake_plan(0, "Вестерн")
    raw["cards"][0]["object_description"] = description
    raw["cards"][0]["materials"] = ["metal"]
    return [PlanDraft.model_validate(raw)]


def test_normalize_plans_rewrites_and_reports():
    result = normalize_plans(RULES, _plans("silver milk churn with a lid"))
    card = result["plans"][0].cards[0]
    assert card.object_description == "soft stylised chrome milk churn with a lid"
    assert result["changes"][0]["card"].endswith("_c01")
    assert result["changes"][0]["rules"] == ["chrome_phrase"]
    assert any("only colour" in e for e in result["errors"]) and not result["warnings"]


@pytest.mark.parametrize("action", ["warn", "fix"])
def test_sole_neutral_actions(action):
    data = raw_config()
    data["sole_neutral"]["action"] = action
    result = normalize_plans(parse_rules(data), _plans("silver milk churn with a lid"))
    assert not result["errors"]
    if action == "warn":
        assert result["warnings"]
    else:
        assert not result["warnings"]
        assert (
            " and soft stylised chrome milk churn" in result["plans"][0].cards[0].object_description
        )


def test_disabled_rules_pass_plans_through():
    data = raw_config() | {"enabled": False}
    plans = _plans("silver milk churn with a lid")
    result = normalize_plans(parse_rules(data), plans)
    assert result["plans"] is plans and not result["changes"] and not result["errors"]


def test_bad_config_is_rejected():
    data = raw_config()
    data["sole_neutral"]["action"] = "maybe"
    with pytest.raises(ValueError, match="sole_neutral.action"):
        parse_rules(data)
    data = raw_config()
    data["rewrite"].append(dict(data["rewrite"][0]))
    with pytest.raises(ValueError, match="ids repeat"):
        parse_rules(data)


def test_fake_plans_stay_valid_and_unchanged():
    """The offline demo plans already follow the rules: no rewrites, no repair round."""
    plans = [PlanDraft.model_validate(fake_plan(i, "Вестерн")) for i in range(3)]
    result = normalize_plans(RULES, plans)
    assert not result["errors"]


def test_plan_task_carries_material_language(tmp_path):
    from cardforge.orchestrator.budget import BudgetLedger  # noqa: F401  (import check only)
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
    assert task["material_language"]["rules"] == list(RULES.hints)
    assert task["material_language"]["version"] == RULES.version


# Debt T7 (2026-11-10): rewrites must not double words or eat the noun.
@pytest.mark.parametrize(
    "text, materials, expected",
    [
        ("school abacus with a honey-brown wooden frame", ["wood"],
         "school abacus with a honey-orange wooden frame"),
        ("caramel brown leather school satchel", ["leather"],
         "caramel-brown leather school satchel"),
        ("warm golden-yellow brass hand bell", ["metal"], "warm golden-yellow brass hand bell"),
        ("warm golden medal on a ribbon", ["metal"], "warm yellow-orange golden medal on a ribbon"),
        ("round blue tin with a lid", ["metal"], "round blue soft stylised chrome tin with a lid"),
        ("red tin can of film", ["metal"], "red tin can of film"),
        ("steel and wood director's chair", ["metal", "wood"],
         "soft stylised chrome and honey-orange wood director's chair"),
    ],
)  # fmt: skip
def test_rewrites_do_not_double_words_or_eat_nouns(text, materials, expected):
    rules = load_rules(ROOT)
    once = normalize_text(rules, text, materials).after
    assert once == expected
    assert normalize_text(rules, once, materials).after == once  # idempotent
