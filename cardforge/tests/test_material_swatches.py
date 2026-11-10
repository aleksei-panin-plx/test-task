"""Material swatches (Q4) and matched references (Q5): behind flags, off by default."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cardforge.contracts.request import CardDraft
from cardforge.knowledge.catalog import load_cards
from cardforge.knowledge.references import (
    ATLAS_ENV,
    MATCHED_ENV,
    MATERIAL_SWATCHES,
    SWATCH_ENV,
    load_atlas,
    load_swatches,
    parse_swatches,
    select_references,
)

ROOT = Path(__file__).resolve().parents[1]
STYLE = ["CardC6_card_19", "CardC6_card_42", "CardC6_card_25", "CardC6_card_03"]


@pytest.fixture(scope="module")
def catalog():
    return load_cards(ROOT, apply_masks=False)


@pytest.fixture
def raw() -> dict:
    return tomllib.loads((ROOT / MATERIAL_SWATCHES).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for e in (ATLAS_ENV, SWATCH_ENV, MATCHED_ENV):
        monkeypatch.delenv(e, raising=False)


def draft(desc="brass trophy cup", category=2, scale="tabletop", materials=("metal",)):
    return CardDraft(
        object_name="Thing",
        object_description=desc,
        theme_rationale="test",
        category=category,
        scale_class=scale,
        object_type="decor",
        composition="centered",
        surface="wood" if category in (2, 3) else None,
        environment="outdoor" if category == 4 else None,
        materials=list(materials),
    )


def test_project_file_valid_and_off(raw, catalog):
    book = load_swatches(ROOT)
    assert book is not None and not book.active() and not book.matched_active()
    atlas = load_atlas(ROOT)
    assert set(book.crops) <= set(atlas.entries)
    for rows in book.crops.values():
        for n, *_ in rows:
            assert any(c.card_ref == f"CardC6_card_{n}" for c in catalog)


def test_off_is_byte_identical(raw, catalog):
    off = parse_swatches(raw)
    for seed in range(30):
        for d in (draft(), draft("steel fan", 1, "handheld"), draft("iron lamp", 4, "structure")):
            assert select_references(catalog, d, STYLE, seed, Path("dataset"), swatches=off) == (
                select_references(catalog, d, STYLE, seed, Path("dataset"))
            )


def test_swatch_replaces_material(raw, catalog):
    on = parse_swatches({**raw, "enabled": True})
    refs = select_references(catalog, draft(), STYLE, 3, Path("dataset"), swatches=on)
    roles = [r.role for r in refs]
    assert "material" not in roles and roles.count("swatch") == 1
    sw = next(r for r in refs if r.role == "swatch")
    assert sw.id == "swatch:gold" and sw.path == "dataset/swatches/gold.png"
    off = select_references(
        catalog, draft(), STYLE, 3, Path("dataset"), swatches=parse_swatches(raw)
    )
    assert len(refs) == len(off)
    assert [r for r in refs if r.role != "swatch"] == [r for r in off if r.role != "material"]


def test_no_sheet_keeps_material(raw, catalog):
    on = parse_swatches({**raw, "enabled": True})
    d = draft("wooden chair", materials=("wood",))
    refs = select_references(catalog, d, STYLE, 3, Path("dataset"), swatches=on)
    assert "swatch" not in [r.role for r in refs] and "material" in [r.role for r in refs]


def test_env_wins(raw, catalog, monkeypatch):
    monkeypatch.setenv(SWATCH_ENV, "1")
    monkeypatch.setenv(MATCHED_ENV, "1")
    book = parse_swatches(raw)
    assert book.active() and book.matched_active()


def test_matched_prefers_scale_and_support(raw, catalog):
    on = parse_swatches({**raw, "matched_refs": True})
    by = {c.card_ref: c for c in catalog}
    d = draft(category=2, scale="tabletop")
    for seed in range(10):
        refs = select_references(catalog, d, STYLE, seed, Path("dataset"), swatches=on)
        cat = [by[r.id] for r in refs if r.role == "category"]
        assert len(cat) == 2 and all(c.category == 2 for c in cat)
        strict = [
            c
            for c in catalog
            if c.category == 2 and c.scale_class == "tabletop" and c.surface == "wood"
        ]
        if len(strict) >= 2:
            assert all(c.scale_class == "tabletop" and c.surface == "wood" for c in cat)
