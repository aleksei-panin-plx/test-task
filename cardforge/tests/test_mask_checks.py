"""Runtime mask checks: safe zone, background simplicity, fill (2026-11-05)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from cardforge.contracts.generation import JudgeOutcome
from cardforge.evaluate import card_qa
from cardforge.evaluate.mask_checks import (
    CHECKS,
    FILL_SMALL,
    SYNTHETIC,
    evaluate,
    load_config,
    mask_checks,
    measure,
    synthetic_backdrop,
)
from cardforge.evaluate.object_mask import fake_masker

ROOT = Path(__file__).resolve().parents[1]
W, H = 430, 480


@pytest.fixture(scope="module")
def config():
    cfg = load_config(ROOT, geometry="v1")  # the v1 bands, kept for rollback
    assert cfg is not None and cfg["version"].startswith("mask-checks-v")
    return cfg


def mask(*boxes, shape="ellipse") -> Image.Image:
    out = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(out)
    for box in boxes:
        getattr(draw, shape)(box, fill=255)
    return out


def card(tmp_path: Path, busy: bool = False, name: str = "final.png") -> Path:
    path = tmp_path / name
    image = (
        Image.effect_noise((W, H), 128).convert("RGB")
        if busy
        else Image.new("RGB", (W, H), (230, 200, 160))
    )
    ImageDraw.Draw(image).ellipse([110, 110, 320, 370], fill=(40, 90, 160))
    image.save(path)
    return path


CENTRED = [110, 110, 320, 370]  # 0.27 of the frame, clear of every UI band


def test_centred_object_on_plain_backdrop_passes_every_check(tmp_path, config):
    for category in (1, 2, 3, 4):
        result = mask_checks(card(tmp_path), mask(CENTRED), category, config)
        assert set(result) == set(CHECKS)
        assert result["safe_zone"]["passed"] is True
        assert result["fill"]["passed"] is True
        assert all(r["config"] == config["version"] for r in result.values())
    assert (
        mask_checks(card(tmp_path), mask(CENTRED), 1, config)["background_simplicity"]["passed"]
        is True
    )


def test_object_in_the_star_band_fails_safe_zone_with_crop_code(tmp_path, config):
    # Starts 2 px below the top edge: not cut by the frame, but deep in the star band.
    result = mask_checks(card(tmp_path), mask([110, 2, 320, 260]), 2, config)
    assert result["safe_zone"]["passed"] is False
    assert result["safe_zone"]["code"] == "crop"
    assert result["safe_zone"]["failed"] == ["safe_overlap_max"]
    assert result["safe_zone"]["value"]["safe_overlap"] > 0.122
    # Scenes may reach the bands much further (category 4).
    assert (
        mask_checks(card(tmp_path), mask([110, 2, 320, 260]), 4, config)["safe_zone"]["passed"]
        is True
    )


def test_busy_background_fails_only_for_categories_1_and_2(tmp_path, config):
    busy = card(tmp_path, busy=True)
    for category in (1, 2):
        check = mask_checks(busy, mask(CENTRED), category, config)["background_simplicity"]
        assert check["passed"] is False and check["code"] == "background"
        assert "background_edges_max" in check["failed"]
    for category in (3, 4):
        check = mask_checks(busy, mask(CENTRED), category, config)["background_simplicity"]
        assert check["passed"] is None and check["required"] is False
        assert check["value"] == "not_applicable"


def test_fill_too_small_asks_to_zoom_in_and_too_large_to_zoom_out(tmp_path, config):
    small = mask_checks(card(tmp_path), mask([155, 170, 275, 312]), 1, config)["fill"]
    assert small["passed"] is False and small["code"] is None
    assert small["fix_text"] == FILL_SMALL
    assert {"mask_area_min"} <= set(small["failed"])
    large = mask_checks(card(tmp_path), mask([5, 60, 425, 470], shape="rectangle"), 1, config)
    assert large["fill"]["passed"] is False and large["fill"]["code"] == "crop"
    assert "mask_area_max" in large["fill"]["failed"]


def test_unknown_is_never_a_pass(tmp_path, config):
    image = card(tmp_path)
    # No masker: not run, does not block (same as `crop`), and is not a pass.
    for check in mask_checks(image, None, 1, config).values():
        assert check["passed"] is None and check["value"] == "not_run"
    # Masks that cannot be trusted → review (required).
    for bad in (
        Image.new("L", (W, H), 0),  # empty
        mask([200, 220, 240, 260]),  # 0.6 % of the frame: a segmentation miss
        Image.new("L", (W, H), 255),  # the whole frame
        mask(*[[x, 200, x + 40, 240] for x in range(10, 400, 60)]),  # shattered
    ):
        for name, check in mask_checks(image, bad, 2, config).items():
            assert check["passed"] is None and check["required"] is True, name
    # No config: a present mask is review, never a silent pass.
    missing = mask_checks(image, mask(CENTRED), 1, None, root=tmp_path)
    assert all(c["passed"] is None and c["required"] for c in missing.values())
    # A category without limits is review, not a pass.
    no_limits = evaluate(measure(Image.open(image), mask(CENTRED), config["zone"]), 5, config)
    assert no_limits["safe_zone"]["passed"] is None and no_limits["safe_zone"]["required"]


def test_measure_keeps_the_stage2_feature_definitions(tmp_path, config):
    from cardforge.evaluate.hard_checks import features

    image = card(tmp_path)
    mask(CENTRED).save(tmp_path / "mask.png")
    stage2 = features(image, tmp_path / "mask.png", config["zone"])
    full = measure(Image.open(image), mask(CENTRED), config["zone"])
    assert stage2 == {k: full[k] for k in stage2}
    assert full["largest_component"] == 1.0 and full["safe_overlap"] == 0.0


def test_fake_masker_marks_synthetic_masks_and_busy_attempts(tmp_path, config):
    attempt = tmp_path / "cards/v01/v01_c03/attempt_02"
    attempt.mkdir(parents=True)
    path = card(attempt, busy=True)  # a placeholder image full of edges
    plain = fake_masker()(path)
    assert plain.info[SYNTHETIC] == "flat"
    assert synthetic_backdrop(plain).getextrema() == ((200, 200), (180, 180), (160, 160))
    assert mask_checks(path, plain, 1, config)["background_simplicity"]["passed"] is True
    busy = fake_masker({"busy_background": {"v01_c03": [2]}})(path)
    assert busy.info[SYNTHETIC] == "busy"
    assert mask_checks(path, busy, 1, config)["background_simplicity"]["passed"] is False
    assert synthetic_backdrop(mask(CENTRED)) is None  # real masks read the real image


def _spec(category: int = 1):
    from cardforge.contracts.request import CardSpec

    fields = CardSpec.model_fields
    data = {
        "card_id": "v01_c01",
        "object_name": "Хлопушка",
        "object_description": "wooden clapperboard",
        "category": category,
        "must_have": [],
    }
    return CardSpec.model_construct(**{k: v for k, v in data.items() if k in fields})


def test_hard_checks_chain_runs_the_mask_checks_and_decide_routes_them(tmp_path, config):
    profile = card_qa.JudgeProfile(id="fake", provider="fake")
    ok_judge = JudgeOutcome(
        judge_model="fake:j", decision="accept", decision_policy=profile.decision_policy
    )
    ocr = {"status": "ok", "observations": []}
    final = card(tmp_path)

    good = card_qa.hard_checks(final, _spec(), ocr, mask(CENTRED), "test", config)
    assert {"format", "text", "crop", *CHECKS} <= set(good.checks)
    assert not good.failed and not good.unknown
    assert card_qa.decide(good, [], ok_judge, profile, {}).decision == "accept"

    # Safe zone alone → a flag for the person (user 2026-11-09), no redraw; the measurement and
    # what the check would have done stay in eval.json.
    band = card_qa.hard_checks(final, _spec(), ocr, mask([110, 15, 320, 260]), "test", config)
    assert not band.failed and band.flagged == ["safe_zone"]
    zone = band.checks["safe_zone"]
    assert zone["measured_passed"] is False and zone["would"] == "fix" and zone["code"] == "crop"
    ev = card_qa.decide(band, [], ok_judge, profile, {})
    assert ev.decision == "flag" and ev.reasons == ["hard_check:safe_zone:flag"] and ev.fixes == []
    legacy = {k: v for k, v in config.items() if k != "flag_only"}  # before 2026-11-09: a fix
    old = card_qa.hard_checks(final, _spec(), ocr, mask([110, 15, 320, 260]), "test", legacy)
    ev = card_qa.decide(old, [], None, profile, {})
    assert ev.decision == "fix" and ev.reasons == ["hard_check:safe_zone"]
    assert ev.fixes == [{"source": "hard_check:safe_zone", "codes": ["crop"]}]

    # Too small → fix with the zoom-in line as text.
    small = card_qa.hard_checks(final, _spec(), ocr, mask([155, 170, 275, 312]), "t", config)
    ev = card_qa.decide(small, [], None, profile, {})
    assert ev.reasons == ["hard_check:fill"]
    assert ev.fixes == [{"source": "hard_check:fill", "codes": [], "text": FILL_SMALL}]

    # Cut by the top edge: one defect, one reason (`crop`, now a flag); safe zone is not a pass.
    cut = card_qa.hard_checks(final, _spec(), ocr, mask([110, -100, 320, 260]), "t", config)
    assert cut.checks["safe_zone"]["passed"] is None
    assert cut.checks["safe_zone"]["subsumed_by"] == "crop"
    assert cut.flagged == ["crop"] and cut.checks["safe_zone"]["would"] == "fix"
    assert card_qa.decide(cut, [], None, profile, {}).reasons == [
        "judge:not_run",
        "hard_check:crop:flag",
    ]

    # Untrusted mask → the card goes to review even with an accepting judge.
    tiny = card_qa.hard_checks(final, _spec(), ocr, mask([200, 220, 240, 260]), "t", config)
    assert set(tiny.unknown) >= {"safe_zone", "fill", "background_simplicity"}
    ev = card_qa.decide(tiny, [], ok_judge, profile, {})
    assert ev.decision == "flag" and "hard_check:fill:unknown" in ev.reasons


# ------------------------------------------------- top crop: headroom (2026-11-06)


def test_top_headroom_catches_a_spire_in_the_star_band_without_edge_contact(tmp_path, config):
    from cardforge.evaluate.object_mask import crop_check, frame_margins, load_frame_limits

    frame = load_frame_limits(ROOT)
    assert frame["all"]["top_soft_min"] == 0.025
    body = [60, 120, 370, 420]
    # Gazebo-like: a narrow finial runs up to 1 % of the height (row 5), no edge contact.
    spire = mask(body, [200, 5, 230, 130], shape="rectangle")
    m = frame_margins(spire)
    assert m["top_soft"] == round(5 / 480, 4)
    check = crop_check(spire, 4, frame=frame)
    assert check["passed"] is False and check["edges"] == ["top_headroom"]
    assert check["value"]["top"] == 0.0  # the phase-1 edge contact does not see it
    # A soft, semi-transparent top (u2net grey ~ 60) counts too.
    soft = mask(body, shape="rectangle")
    ImageDraw.Draw(soft).rectangle([150, 2, 280, 119], fill=60)
    assert crop_check(soft, 4, frame=frame)["edges"] == ["top_headroom"]
    # Clear headroom passes; the limit can be switched off in the config.
    assert crop_check(mask(body, shape="rectangle"), 4, frame=frame)["passed"] is True
    assert crop_check(spire, 4, frame={})["passed"] is True


def test_top_crop_goes_through_the_chain_as_crop(tmp_path, config):
    profile = card_qa.JudgeProfile(id="fake", provider="fake")
    ocr = {"status": "ok", "observations": []}
    spire = mask([60, 120, 370, 420], [200, 5, 230, 130], shape="rectangle")
    hard = card_qa.hard_checks(card(tmp_path), _spec(4), ocr, spire, "test", config)
    assert hard.checks["crop"]["passed"] is None and hard.checks["crop"]["measured_passed"] is False
    assert "top_headroom" in hard.checks["crop"]["edges"] and hard.flagged == ["crop"]
    ev = card_qa.decide(hard, [], None, profile, {})
    assert ev.decision == "flag" and "hard_check:crop:flag" in ev.reasons and ev.fixes == []


# ------------------------------------- bottom contact by category (variant A, 2026-11-08)


def _standing(width_px: int) -> Image.Image:
    """A body clear of every edge plus a foot of `width_px` columns down to the bottom."""
    return mask([120, 100, 310, 400], [200, 400, 200 + width_px - 1, 479], shape="rectangle")


def test_bottom_contact_limits_come_from_the_config_by_category(config):
    from cardforge.evaluate.object_mask import LIMITS, crop_check

    assert config["crop"]["standing"]["bottom"] == 0.10
    assert config["crop"]["floating"]["bottom"] == 0.02
    for category in (2, 3):
        five = crop_check(_standing(22), category)  # 22 / 430 = 5.1 % of the width
        assert five["value"]["bottom"] > 0.05 and five["passed"] is True
        twelve = crop_check(_standing(52), category)  # 12.1 %
        # Measured as a bottom cut, but bottom contact no longer flags (2026-11-09).
        assert twelve["measured_edges"] == ["bottom"] and twelve["unflagged_would"] == "fix"
        assert twelve["passed"] is True and twelve["edges"] == []
        assert twelve["threshold"]["bottom"] == ">= 0.1"
    three = crop_check(_standing(13), 1)  # category 1 floats: 3 % already cuts
    assert 0.02 < three["value"]["bottom"] < 0.05
    assert three["measured_edges"] == ["bottom"] and three["passed"] is True
    # Without `flag_edges` every measured edge flags (variant A limits as before).
    every_edge = {k: v for k, v in config["crop"].items() if k != "flag_edges"}
    assert crop_check(_standing(52), 2, edges=every_edge)["edges"] == ["bottom"]
    assert crop_check(_standing(13), 1, edges=every_edge)["passed"] is False
    # Without a `[crop.*]` group the phase-1 defaults apply (2 % for every object).
    assert LIMITS["object"]["bottom"] == 0.02
    assert crop_check(_standing(22), 2, frame={}, edges={})["passed"] is False


# ------------------------------------------ safe zone v2: real frame (proposal, 2026-11-09)


def test_frame_geometry_v2_is_on_by_default_and_uses_the_real_occlusion(tmp_path):
    from cardforge.evaluate.mask_checks import bands

    v2 = load_config(ROOT)  # user «Да, все три», 2026-11-09
    assert v2["frame_geometry"] == "v2" and v2["version"] == "mask-checks-v2.1"
    assert v2["safe_zone"]["objects"]["safe_overlap_max"] == 0.0112
    assert v2["safe_zone"]["scenes"]["safe_overlap_max"] == 0.0932
    assert v2["crop"]["flag_edges"] == ["top", "top_headroom"]
    v1 = load_config(ROOT, geometry="v1")  # rollback
    assert v1["frame_geometry"] == "v1" and v1["version"] == "mask-checks-v1"
    assert v1["safe_zone"]["objects"]["safe_overlap_max"] == 0.122
    assert v2["crop"] == v1["crop"] and v2["frame"] == v1["frame"]  # art edge: unchanged
    assert "top_width_min" not in v2["frame"]["all"]  # the tolerance is only a proposal
    top, bottom, side = bands(H, W, v2["zone"])
    assert not bottom.any() and not side.any()  # the plaque does not cover the art
    assert 0.05 < top.mean() < 0.08  # stars + rounded top corners only
    assert top[0, 0] and top[50, 0] and not top[60, 0]  # rounded top-left corner
    assert top[20, W // 2] and not top[60, W // 2]  # a star in the middle, ~7 % deep


def test_v2_flags_an_object_under_the_stars_but_not_one_on_the_plaque_line(tmp_path):
    v1, v2 = load_config(ROOT, geometry="v1"), load_config(ROOT)
    image = card(tmp_path)
    # Bottom band of v1 (11 %): the object stands on the window bottom, plaque is below it.
    low = mask([110, 200, 320, 475], shape="rectangle")
    assert mask_checks(image, low, 1, v1)["safe_zone"]["passed"] is False
    assert mask_checks(image, low, 1, v2)["safe_zone"]["passed"] is True
    # Under the middle star (top 30 px in the centre) the frame does cover the object.
    high = mask([180, 12, 250, 300], shape="rectangle")
    check = mask_checks(image, high, 1, v2)["safe_zone"]
    assert check["passed"] is False and check["config"] == "mask-checks-v2.1"


def test_v2_tolerates_a_small_part_near_the_top_but_not_a_wide_one():
    from cardforge.evaluate.object_mask import crop_check, top_protrusion

    config = load_config(ROOT)
    v1 = config["frame"]  # active: any part near the top counts
    v2 = {"all": {**v1["all"], "top_width_min": config["tolerance_proposal"]["top_width_min"]}}
    body = [120, 80, 310, 400]  # 191 px wide, clear of every edge
    antenna = mask(body, [210, 3, 219, 80], shape="rectangle")  # 10 px = 5 % of the width
    assert top_protrusion(antenna)["top_width"] < 0.10
    assert crop_check(antenna, 1, frame=v1)["edges"] == ["top_headroom"]  # active rule
    assert crop_check(antenna, 1, frame=v2)["passed"] is True  # proposal: small is fine
    roof = mask(body, [150, 3, 280, 80], shape="rectangle")  # 131 px = 69 % of the width
    assert top_protrusion(roof)["top_width"] > 0.5
    assert crop_check(roof, 1, frame=v2)["edges"] == ["top_headroom"]


def test_prompt_and_crop_use_the_frame_geometry_of_the_checks(tmp_path):
    """The prompt bands and the crop window come from the same frame as the checks (v2: stars on
    top, nothing at the bottom and sides); `frame_geometry = "v1"` rolls all of them back."""
    import shutil

    from cardforge.contracts.request import SafeZone
    from cardforge.evaluate.mask_checks import render_safe_zone
    from cardforge.export.postprocess import crop_window

    v2 = render_safe_zone(ROOT)
    assert v2 == {"top_pct": 0.0771, "bottom_pct": 0.0, "side_pct": 0.0}
    for rel in ("config/mask_checks.toml", "knowledge/frames/safe_zone.json",
                "knowledge/frames/safe_zone.v2.json"):  # fmt: skip
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    cfg = tmp_path / "config/mask_checks.toml"
    cfg.write_text(cfg.read_text().replace('frame_geometry = "v2"', 'frame_geometry = "v1"'))
    assert render_safe_zone(tmp_path) == {"top_pct": 0.14, "bottom_pct": 0.11, "side_pct": 0.08}
    box = (400, 700, 624, 1100)  # object centre y = 900 in a 1024 × 1536 raw image
    top_v2 = crop_window(1024, 1536, box, SafeZone(**v2))[1]
    top_v1 = crop_window(1024, 1536, box, SafeZone(**render_safe_zone(tmp_path)))[1]
    assert top_v2 < top_v1  # the centre of the free window is lower: no plaque band below
