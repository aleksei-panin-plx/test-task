"""ref-v6: light/colour/background blocks and the per-card material table (2026-11-06).

Source: docs/research/generation-defects.md §5. ref-v6 is opt-in; ref-v5 prompts must not change.
"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from cardforge.contracts.generation import Reference
from cardforge.contracts.request import Background, CardSpec
from cardforge.llm.fake import fake_plan
from cardforge.orchestrator.workspace import Workspace, render_profile
from cardforge.policy.loader import resolve_active
from cardforge.prompting.assemble import build_bundle, load_template, material_lines

ROOT = Path(__file__).resolve().parents[1]
V6 = "prompts/render/ref-v6.toml"
V5 = "prompts/render/ref-v5.toml"
REFS = [
    Reference(id="CardC6_card_17", role="style", path="dataset/CardC6_card_17.png"),
    Reference(id="CardC6_card_47", role="background", path="x/CardC6_card_47.png"),
]


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


def spec(materials: list[str], description: str, category: int = 1) -> CardSpec:
    draft = fake_plan(0, "Вестерн")["cards"][2]
    return CardSpec(
        **{**draft, "materials": materials, "object_description": description,
           "category": category, "background": Background(motif="rays", color="orange"),
           "surface": "wooden table" if category in (2, 3) else None,
           "environment": "workshop" if category == 4 else None},
        card_id="v01_c03",
        safe_zone={"top_pct": 0.14, "bottom_pct": 0.11, "side_pct": 0.08},
        reference_roles={},
        style_profile_id="reference_cards_v1",
        seed=7,
    )  # fmt: skip


def prompt(ws, template: str, s: CardSpec) -> str:
    profile = replace(render_profile(ws, "or-gpt-image-2-5"), template=template)
    return build_bundle(
        load_template(ROOT, template), profile, s, resolve_active(ROOT, [], []), [],
        seed=1, references=REFS, max_refs=6, motif_texts={"rays": "two-tone rays, lighter centre"},
    ).prompt_text  # fmt: skip


def table() -> dict:
    return tomllib.loads((ROOT / V6).read_text())


def test_ref_v6_drops_the_phrases_that_caused_sharp_highlights_and_haze(ws):
    for category in (1, 2, 3, 4):
        text = prompt(ws, V6, spec(["metal", "wood"], "a red tin lantern", category))
        low = text.lower()
        for bad in ("specular", "crisp highlights", "soft rim light", "moderately desaturated",
                    "less saturated than the object"):  # fmt: skip
            assert bad not in low, bad
        for good in ("Light: one soft key light", "No rim light", "Colour: every part",
                     "no grey haze", "clean saturated orange backdrop", "slightly lighter behind",
                     "no washed-out filter", "two-tone rays"):  # fmt: skip
            assert good in text, good
    v5 = prompt(ws, V5, spec(["metal"], "a red lantern"))
    assert "sharp bright specular highlights" in v5 and "moderately desaturated" in v5


def test_material_lines_only_for_the_card_materials():
    data = table()
    m = data["materials"]
    lines = material_lines(data, spec(["wood", "fabric"], "a wooden chair with a cushion"))
    assert lines == [m["wood"], m["fabric"]]
    assert m["metal"] not in lines and m["glass"] not in lines
    # unknown vocabulary words get no line; no materials, no block
    assert material_lines(data, spec(["food", "organic"], "a loaf of bread")) == []
    assert material_lines(data, spec([], "a thing")) == []
    # case and duplicates
    assert material_lines(data, spec(["Wood", "wood"], "a plank")) == [m["wood"]]


def test_metal_variants_follow_words_in_the_description():
    data = table()
    m, var = data["materials"], data["material_variants"]["metal"]
    plain = material_lines(data, spec(["metal"], "a red painted toolbox"))
    assert plain == [m["metal"]]
    assert material_lines(data, spec(["metal"], "a polished brass telescope")) == [
        m["metal"], var["gold"]["text"]]  # fmt: skip
    assert var["chrome"]["text"] in material_lines(data, spec(["metal"], "a Steel whisk"))
    assert var["iron"]["text"] in material_lines(data, spec(["metal"], "a cast iron kettle"))
    both = material_lines(data, spec(["metal"], "a silver cup with golden handles"))
    assert var["gold"]["text"] in both and var["chrome"]["text"] in both
    # whole words only: «tinted», «painting», «ironic» do not trigger a variant
    assert material_lines(data, spec(["metal"], "a tinted painting easel, ironic")) == [m["metal"]]
    # a variant word without metal in spec.materials adds nothing
    assert material_lines(data, spec(["wood"], "a wooden box with brass hinges")) == [m["wood"]]


def test_ref_v6_prompt_places_material_block_after_the_object(ws):
    text = prompt(ws, V6, spec(["metal", "glass"], "a brass lamp with a glass shade"))
    lines = text.split("\n")
    i = next(n for n, x in enumerate(lines) if x.startswith("Main subject:"))
    assert lines[i + 1].startswith("Materials: Metal is mostly painted enamel")
    assert "Gold and brass" in lines[i + 1] and "Glass is a clean flat light tint" in lines[i + 1]
    assert "Chrome and steel" not in text and "Wood is" not in text
    assert "Materials:" not in prompt(ws, V6, spec(["food"], "a loaf of bread"))


def test_ref_v5_prompts_unchanged(ws):
    text = prompt(ws, V5, spec(["metal", "wood"], "a brass lamp"))
    assert "Materials:" not in text and "Light:" not in text and "Colour:" not in text
    assert load_template(ROOT, V5).version == "ref-v5@06336ffcba16"


def test_edit_pass_and_material_fix_follow_ref_v6():
    """The edit pass and the `material` fix line go into the same ref-v6 cards: they no longer ask
    for the sharp highlights and rim light that ref-v6 forbids (restyle-v2, fixes-v3)."""
    from cardforge.render import edit_pass

    fixes = tomllib.loads((ROOT / "prompts/render/fixes.toml").read_text())
    for text in (edit_pass.INSTRUCTION, fixes["codes"]["material"]["prompt"]):
        low = text.lower()
        for bad in ("specular", "crisp", "soft rim light", "polished metal"):
            assert bad not in low, (bad, text[:60])
    assert edit_pass.INSTRUCTION_VERSION == "restyle-v2" and "no rim light" in edit_pass.INSTRUCTION
    assert "crisp bright specular" in edit_pass.INSTRUCTION_V1  # the Q3 wording stays on record
    assert fixes["version"] == "fixes-v3"
