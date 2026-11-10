"""Look signals (haze, pure colour, small highlights) and the look checklist items behind a flag."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from cardforge.contracts.request import CardSpec
from cardforge.evaluate import card_qa, look_checklist, look_features, veto_judge

ROOT = Path(__file__).resolve().parents[1]


def mask_img(box=(115, 120, 315, 360)) -> Image.Image:
    m = Image.new("L", (430, 480), 0)
    ImageDraw.Draw(m).ellipse(box, fill=255)
    return m


def card(colour=(230, 40, 30), backdrop=(30, 90, 220), spots=0, veil=0.0) -> Image.Image:
    im = Image.new("RGB", (430, 480), backdrop)
    d = ImageDraw.Draw(im)
    d.ellipse((115, 120, 315, 360), fill=colour)
    for i in range(spots):  # small bright highlights on the object
        x, y = 160 + (i % 6) * 22, 170 + (i // 6) * 30
        d.ellipse((x, y, x + 4, y + 4), fill=(255, 255, 255))
    a = np.asarray(im).astype(float)
    a = a + (255 - a) * veil  # milky veil: raises min(R, G, B)
    return Image.fromarray(a.clip(0, 255).astype("uint8"))


def test_haze_and_saturation_follow_the_veil():
    clean = look_features.measure(card(), mask_img())
    milky = look_features.measure(card(veil=0.35), mask_img())
    assert milky["haze"] > clean["haze"] + 0.2
    assert milky["obj_haze"] > clean["obj_haze"]
    assert clean["sat_p90"] > 0.8 > milky["sat_p90"]


def test_small_highlights_are_counted_on_the_object_only():
    none = look_features.measure(card(), mask_img())["highlights"]
    many = look_features.measure(card(spots=18), mask_img())["highlights"]
    assert none["count"] == 0
    assert many["count"] == 18
    assert 4 <= many["size_median"] <= 40
    # Outside the mask the same spots are not object highlights.
    off = look_features.measure(card(spots=18), mask_img((0, 0, 60, 60)))["highlights"]
    assert off["count"] == 0


def test_no_mask_keeps_frame_features_only():
    f = look_features.measure(card(), None)
    assert f["haze"] is not None and f["highlights"] is None and f["phase4_flag"] is None


def test_phase4_flag():
    assert look_features.measure(card(), mask_img((200, 220, 260, 280)))["phase4_flag"] is True
    assert look_features.measure(card(), mask_img((40, 40, 390, 440)))["phase4_flag"] is False


def test_reference_percentiles_from_config():
    config = look_features.load_config(ROOT)
    assert config and config["version"] == look_features.VERSION
    pct = look_features.ref_percentiles({"haze": 0.24, "sat_p90": 2.0}, config)
    assert 30 < pct["haze"] < 70 and pct["sat_p90"] == 100.0


def test_look_is_a_signal_in_hard_checks_and_never_decides(tmp_path):
    path = tmp_path / "final.png"
    card(veil=0.5, spots=30).save(path)
    spec = CardSpec.model_construct(
        card_id="v01_c01", object_name="кубок", category=1, must_have=[]
    )
    hard = card_qa.hard_checks(path, spec, {"status": "ok", "observations": []}, mask_img())
    look = hard.checks["look"]
    assert look["signal_only"] is True and look["passed"] is None and look["required"] is False
    assert look["value"]["highlights"]["count"] == 30 and "haze" in look["ref_pct"]
    assert "look" not in hard.failed and "look" not in hard.unknown
    json.dumps(look)  # journal-safe
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    entry = look_features.look_signal(broken, None)
    assert entry["value"] == "not_run" and entry["passed"] is None


# ---------------------------------------------------------------- checklist items


def test_v1_contract_is_unchanged_without_the_flag():
    task = veto_judge.veto_task(
        card_id="c", attempt_no=1, image="i.png", image_sha256="x", spec={}, refs=[], ref_sha256=[]
    )
    assert task["instruction"] == veto_judge.CHECKLIST_PROMPT
    assert task["answer_schema"] == veto_judge.ChecklistAnswer.model_json_schema()
    assert task["schema_version"] == veto_judge.PROFILE
    assert veto_judge.checklist("defect-checklist-v1") is veto_judge.BASE


def _answer(names, hit=()):
    return {"extra_items": [], **{k: {"defect": k in hit, "note": "n"} for k in names}}


def test_look_profile_appends_two_items_with_the_same_contract():
    look = veto_judge.LOOK
    assert look.profile == "defect-checklist-v1+look-v1"
    assert look.prompt.startswith(veto_judge.CHECKLIST_PROMPT + "\n- material_style: ")
    fields = list(look.schema.model_fields)
    assert fields[: len(veto_judge.ChecklistAnswer.model_fields)] == list(
        veto_judge.ChecklistAnswer.model_fields
    )
    assert fields[-2:] == ["material_style", "highlights_light"]
    task = veto_judge.veto_task(
        card_id="c",
        attempt_no=1,
        image="i.png",
        image_sha256="x",
        spec={},
        refs=[],
        ref_sha256=[],
        judge_profile=look.profile,
    )
    assert task["instruction"] == look.prompt and task["schema_version"] == look.profile
    with pytest.raises(ValueError):
        look_checklist.extend(look)


def test_look_items_are_signal_only_in_the_outcome():
    names = list(veto_judge.LOOK.defects)
    out, rec = veto_judge.veto_outcome(_answer(names, {"material_style"}), "p", "m")
    assert out.decision == "accept" and rec["veto"] is False
    assert rec["look_defects"] == ["material_style"] and rec["look_codes"] == ["material"]
    out, rec = veto_judge.veto_outcome(_answer(names, {"artifacts", "highlights_light"}), "p", "m")
    assert out.decision == "reject" and rec["defects"] == ["artifacts"]
    assert rec["look_codes"] == ["style"]
    # A plain v1 answer is still validated by the v1 schema; extra keys alone are invalid.
    out, rec = veto_judge.veto_outcome(_answer(list(veto_judge.DEFECTS)), "p", "m")
    assert out.decision == "accept" and "look_defects" not in rec
    bad = {**_answer(list(veto_judge.DEFECTS)), "material_style": {"defect": True, "note": "n"}}
    assert veto_judge.veto_outcome(bad, "p", "m")[0].decision == "flag"


def test_v2_checklist_of_kimi_and_deepseek_extends_byte_for_byte():
    import importlib.util

    path = ROOT / "runs/stage04/judge-research/phase3.py"
    if not path.is_file():
        pytest.skip("phase-3 research script not present")
    spec = importlib.util.spec_from_file_location("phase3_look_test", path)
    p3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p3)
    base = look_checklist.Checklist(
        p3.V2_PROFILE, p3.V2_PROMPT, p3.ChecklistV2, p3.V2_DEFECTS, p3.V2_CODES
    )
    look = look_checklist.extend(base)
    assert look.profile == "defect-checklist-v2+look-v1"
    assert look.prompt.startswith(p3.V2_PROMPT + "\n")
    assert list(look.schema.model_fields)[-2:] == list(look_checklist.LOOK_ITEMS)
    assert look_checklist.LOOK_VETO == []
