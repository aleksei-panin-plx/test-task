"""Material atlas (H5): curated material references behind a flag, off by default."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cardforge.contracts.request import CardDraft
from cardforge.knowledge.catalog import load_cards
from cardforge.knowledge.references import (
    ATLAS_ENV,
    MATERIALS_ATLAS,
    atlas_problems,
    load_atlas,
    parse_atlas,
    select_references,
)

ROOT = Path(__file__).resolve().parents[1]
STYLE = ["CardC6_card_19", "CardC6_card_42"]


@pytest.fixture(scope="module")
def catalog():
    return load_cards(ROOT, apply_masks=False)


@pytest.fixture
def raw() -> dict:
    return tomllib.loads((ROOT / MATERIALS_ATLAS).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(ATLAS_ENV, raising=False)


def draft(materials, description=None, name="Thing", category=1, scale="handheld"):
    return CardDraft(
        object_name=name,
        object_description=description,
        theme_rationale="test",
        category=category,
        scale_class=scale,
        object_type="tool",
        composition="centered",
        surface="table" if category in (2, 3) else None,
        environment="outdoor" if category == 4 else None,
        materials=materials,
    )


def on(raw: dict, **extra):
    return parse_atlas({**raw, "enabled": True, **extra})


def test_project_atlas_is_valid_and_off_by_default(raw, catalog):
    atlas = load_atlas(ROOT)
    assert atlas is not None and atlas.enabled is False and not atlas.active()
    assert atlas_problems(atlas, catalog) == []
    for e in atlas.entries.values():
        assert 2 <= len(e.refs) <= 3, e.key
        assert len(set(e.refs)) == len(e.refs)
    assert {
        "painted_metal",
        "gold",
        "chrome",
        "dark_iron",
        "wood",
        "glass",
        "fabric",
        "stone",
        "water",
        "sky",
        "grass",
        "trees",
        "flowers",
        "cars",
    } <= set(atlas.entries)


@pytest.mark.parametrize(
    ("materials", "description", "key"),
    [
        (["metal"], "red mailbox", "painted_metal"),
        (["metal", "glass"], "polished brass spyglass with steel rings", "gold"),
        (["metal"], "chrome desk fan", "chrome"),
        (["metal"], "old cast iron kettle", "dark_iron"),
        (["Brass"], "steel bell", "gold"),  # explicit material beats description words
        (["steel"], None, "chrome"),
        (["food", "wood"], "picnic table", "wood"),  # first material the atlas knows
        (["organic", "wood"], "flower arch with roses", "flowers"),  # organic refined
        (["organic", "wood"], "garden arch", "wood"),  # organic, no keyword -> next material
        (["organic"], "palm tree on sand", "trees"),
        (["organic", "stone"], "grassy mound", "grass"),
        (["food", "water"], "soup", "water"),
        (["metal", "rubber"], "yellow taxi car", "cars"),  # subject beats material
        (["metal", "glass"], "white airplane among clouds", "sky"),
        (["stone", "water"], "garden pond with lilies", "water"),  # "pond" before "lilies"
        (["food"], "sandwich", None),
        ([], "anything", None),
    ],
)
def test_main_material(raw, materials, description, key):
    assert on(raw).main_material(draft(materials, description)) == key


def test_keyword_needs_whole_word(raw):
    # "ironing" is not "iron"
    assert on(raw).main_material(draft(["metal"], "ironing board")) == "painted_metal"


def test_flag_off_matches_previous_selection(raw, catalog):
    spec = draft(["metal"], "brass trophy cup", category=2, scale="tabletop")
    default = select_references(catalog, spec, STYLE, 7, Path("dataset"))
    explicit_off = select_references(catalog, spec, STYLE, 7, Path("dataset"), parse_atlas(raw))
    assert default == explicit_off
    assert [r.role for r in default].count("material") == 1


def test_flag_on_uses_curated_material_refs(raw, catalog):
    atlas = on(raw)
    gold = set(atlas.entries["gold"].refs)
    for seed in range(20):
        spec = draft(["metal"], "brass compass", category=1)
        refs = select_references(catalog, spec, STYLE, seed, Path("dataset"), atlas)
        material = [r for r in refs if r.role == "material"]
        others = {r.id for r in refs if r.role != "material"}
        assert len(material) == 1
        if gold - others:  # all curated refs taken by style/category -> old random match
            assert material[0].id in gold
        assert len({r.id for r in refs}) == len(refs)  # no duplicates across roles
        assert material[0].path == f"dataset/{material[0].id}.png"
        # Style and category are unchanged by the flag.
        off = select_references(catalog, spec, STYLE, seed, Path("dataset"), parse_atlas(raw))
        assert [r for r in refs if r.role != "material"] == [r for r in off if r.role != "material"]


def test_flag_on_is_deterministic_and_rotates(raw, catalog):
    atlas = on(raw)
    spec = draft(["wood"], "wooden stool", category=3, scale="furniture")
    picks = {
        seed: next(
            r.id
            for r in select_references(catalog, spec, STYLE, seed, Path("dataset"), atlas)
            if r.role == "material"
        )
        for seed in range(30)
    }
    again = select_references(catalog, spec, STYLE, 5, Path("dataset"), atlas)
    assert next(r.id for r in again if r.role == "material") == picks[5]
    assert len(set(picks.values())) > 1  # not always the first curated card


def test_per_card_and_used_refs_are_respected(raw, catalog):
    atlas = on(raw, per_card=2)
    spec = draft(["metal"], "golden goblet", category=2, scale="tabletop")
    style = ["CardC6_card_19"]  # a gold atlas ref already used as the style reference
    refs = select_references(catalog, spec, style, 3, Path("dataset"), atlas)
    material = [r.id for r in refs if r.role == "material"]
    assert len(material) == 2 and "CardC6_card_19" not in material


def test_unknown_material_falls_back_to_random_match(raw, catalog):
    atlas = on(raw)
    spec = draft(["food"], "sandwich")
    a = select_references(catalog, spec, STYLE, 11, Path("dataset"), atlas)
    b = select_references(catalog, spec, STYLE, 11, Path("dataset"), parse_atlas(raw))
    assert a == b


def test_env_overrides_file_flag(raw, monkeypatch):
    monkeypatch.setenv(ATLAS_ENV, "1")
    assert parse_atlas(raw).active()
    monkeypatch.setenv(ATLAS_ENV, "0")
    assert not on(raw).active()


def test_parse_rejects_bad_atlas(raw):
    no_kw = {**raw, "materials": {**raw["materials"], "x": {"refines": ["metal"], "refs": ["a"]}}}
    with pytest.raises(ValueError, match="keywords"):
        parse_atlas(no_kw)
    dup = {**raw, "materials": {**raw["materials"], "x": {"aliases": ["wood"], "refs": ["a"]}}}
    with pytest.raises(ValueError, match="alias"):
        parse_atlas(dup)


def test_problems_flag_unknown_and_character_refs(raw, catalog):
    gold_char = next(c.card_ref for c in catalog if c.category == 5)
    bad = {**raw, "materials": {**raw["materials"]}}
    bad["materials"]["wood"] = {**raw["materials"]["wood"], "refs": ["CardC6_card_999", gold_char]}
    problems = atlas_problems(parse_atlas(bad), catalog)
    assert any("999" in p for p in problems) and any("category-5" in p for p in problems)
