"""«Снять фильтр» (dehaze) post-processing step, experiment E1 (2026-11-06)."""

import io
import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from cardforge.contracts.request import CollectionRequest, Hitl, SafeZone
from cardforge.export import postprocess
from cardforge.export.postprocess import (
    DehazeParams,
    dehaze,
    dehaze_params_for,
    finalize,
    haze_stats,
    levels_lut,
    preset_params,
)
from cardforge.orchestrator import service
from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
ZONE = SafeZone(top_pct=0.14, bottom_pct=0.11, side_pct=0.08)


def veiled_card(veil: float = 0.4) -> Image.Image:
    """A saturated card (object + backdrop + pure white + dark) under a milky veil."""
    im = Image.new("RGB", (430, 480), (40, 120, 200))
    d = ImageDraw.Draw(im)
    d.ellipse((120, 140, 310, 360), fill=(220, 60, 30))
    d.rectangle((180, 200, 250, 260), fill=(255, 255, 255))
    d.rectangle((150, 330, 280, 350), fill=(30, 20, 60))
    return im.point(lambda v: round(v * (1 - veil) + 255 * veil))


def write_config(root: Path, text: str) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/postprocess.toml").write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- config: off by default


def test_project_config_is_on_only_for_nano_and_presets_parse():
    # 2026-11-06: user switched nano-v2 on for Nano Banana only (AGENTS.md decision log).
    runtime = tomllib.loads((ROOT / "config/runtime.toml").read_text(encoding="utf-8"))
    for pid in runtime["render_profiles"]:
        if pid == "or-nano-banana-v2":
            assert dehaze_params_for(ROOT, pid) is not None
        else:
            assert dehaze_params_for(ROOT, pid) is None
    cfg = postprocess.load_dehaze_config(ROOT)
    assert cfg["profiles"] == {"or-nano-banana-v2": "nano-v2"}
    for name, data in cfg["presets"].items():
        p = preset_params(data, name)
        assert 1.1 <= p.chroma <= 1.2 and p.bg_guard


def test_missing_config_means_off(tmp_path):
    assert dehaze_params_for(tmp_path, "or-nano-banana-v2") is None
    assert dehaze_params_for(ROOT, None) is None


def test_profile_listed_in_config_gets_its_preset(tmp_path):
    root = write_config(
        tmp_path,
        '[dehaze.profiles]\n"or-nano-banana-v2" = "p1"\n'
        "[dehaze.presets.p1]\nstrength = 0.5\nchroma = 1.2\n",
    )
    p = dehaze_params_for(root, "or-nano-banana-v2")
    assert p == DehazeParams(preset="p1", strength=0.5, chroma=1.2)
    assert dehaze_params_for(root, "or-gpt-image-2-5") is None


def test_bad_config_fails_loudly(tmp_path):
    root = write_config(tmp_path, '[dehaze.profiles]\nx = "missing"\n')
    with pytest.raises(ValueError, match="not in config/postprocess.toml"):
        dehaze_params_for(root, "x")
    with pytest.raises(ValueError, match="unknown dehaze parameters"):
        preset_params({"strenght": 1.0}, "typo")
    with pytest.raises(ValueError):
        DehazeParams(chroma=2.0)
    with pytest.raises(ValueError):
        DehazeParams(toe=1.5)


# ---------------------------------------------------------------- the curve


@pytest.mark.parametrize("black,toe", [(0.1, 3.0), (0.25, 2.0), (0.3, 2.5)])
def test_levels_curve_keeps_white_and_black_and_is_monotone(black, toe):
    lut = levels_lut(black, toe)
    assert lut[0] == 0 and lut[255] == 255
    assert all(b >= a for a, b in zip(lut, lut[1:], strict=False))
    assert lut[128] < 128  # the veil is removed from the mid tones
    assert levels_lut(0.0, toe) == list(range(256))


def test_toe_deepens_darks_without_a_hard_clip():
    lut = levels_lut(0.2, 3.0)
    # a hard levels cut would send everything below 0.2·255 = 51 to 0
    assert all(lut[v] > 0 for v in range(8, 52))


# ---------------------------------------------------------------- the step


def test_dehaze_removes_the_veil_and_protects_whites():
    im = veiled_card(0.4)
    before = haze_stats(im)
    out, info = dehaze(im, DehazeParams())
    after = haze_stats(out)
    assert info["before"] == before and info["after"] == after
    assert after["haze"] < before["haze"] - 0.1
    assert after["sat_p90"] > before["sat_p90"]
    assert out.getpixel((215, 230)) == (255, 255, 255)  # pure white stays white
    dark_before, dark_after = im.getpixel((215, 340)), out.getpixel((215, 340))
    assert sum(dark_after) < sum(dark_before) and max(dark_after) > 0  # deeper, not clipped
    assert info["black"] == pytest.approx(min(0.25, (before["haze"] - 0.24) / 0.76), abs=1e-3)
    assert info["background_strength"] == 1.0
    json.dumps(info)  # recorded in postprocess.json


def test_clean_image_gets_no_levels_change():
    im = veiled_card(0.0)
    assert haze_stats(im)["haze"] < 0.24
    out, info = dehaze(im, DehazeParams(chroma=1.0))
    assert info["black"] == 0.0
    assert out.tobytes() == im.tobytes()


def test_background_guard_keeps_the_contrast():
    # A near-black object on a dim backdrop: the toe pulls the backdrop down to the object
    # and would cut their separation, so the guard keeps (some of) the original backdrop.
    im = Image.new("RGB", (430, 480), (95, 95, 105))
    ImageDraw.Draw(im).ellipse((140, 160, 290, 330), fill=(15, 12, 12))
    p = DehazeParams(chroma=1.0, max_black=0.45, toe=2.0, target_haze=0.0)
    out, info = dehaze(im, p)
    sep = info["separation_delta_e"]
    assert sep["1.0"] < sep["before"] - p.bg_guard_tolerance
    assert info["background_strength"] < 1.0
    unguarded_img, unguarded = dehaze(im, replace(p, bg_guard=False))
    assert unguarded["background_strength"] == 1.0 and unguarded["separation_delta_e"] is None
    assert out.getpixel((10, 10)) != unguarded_img.getpixel((10, 10))


# ---------------------------------------------------------------- finalize and wiring


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_finalize_without_params_is_unchanged_and_with_params_records_dehaze():
    raw = _png(veiled_card(0.4).resize((864, 960)))
    plain, info = finalize(raw, ZONE)
    assert "dehaze" not in info
    assert plain == finalize(raw, ZONE, None)[0]
    data, info = finalize(raw, ZONE, DehazeParams(preset="nano-v1"))
    assert info["dehaze"]["params"]["preset"] == "nano-v1"
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == (430, 480)
        assert haze_stats(out)["haze"] < info["dehaze"]["before"]["haze"]


def test_service_asks_for_the_render_profile_of_each_card(tmp_path, monkeypatch):
    seen = []

    def fake_params(root, profile_id):
        seen.append(profile_id)
        return DehazeParams(preset="t") if profile_id == "fake" else None

    monkeypatch.setattr(service, "dehaze_params_for", fake_params)
    request = CollectionRequest(
        request_id="t", collection_theme="Кино", set_theme="Вестерн",
        producer_wishes_raw="побольше ретро", render_profile="fake",
        hitl=Hitl(g1="none", g2="none"),
    )  # fmt: skip
    ws = Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))
    rid = service.create_run(ws, request, llm="fake", render="fake", judge="fake", run_id="dh")
    service.advance(ws, rid)
    files = ws.run_dir(rid).glob("cards/*/*/attempt_*/postprocess.json")
    infos = [json.loads(p.read_text()) for p in files]
    assert infos and set(seen) == {"fake"}
    assert all(i["dehaze"]["params"]["preset"] == "t" for i in infos)


# ---------------------------------------------------------------- v2: soft shadows, light frames


def test_v2_defaults_are_v1():
    p = DehazeParams()
    assert p.shadow_protect == 0.0 and p.light_luma == 1.0 and p.light_min == 1.0
    assert postprocess.light_factor(0.9, p) == 1.0


def test_shadow_protect_keeps_dark_shadows_soft():
    im = veiled_card(0.15)  # dark band ≈ (64, 55, 89): a shadow, not a veil
    hard, _ = dehaze(im, DehazeParams(max_black=0.3, toe=2.0, chroma=1.0, target_haze=0.0))
    soft, _ = dehaze(im, DehazeParams(max_black=0.3, toe=2.0, chroma=1.0, target_haze=0.0,
                                      shadow_protect=0.4))  # fmt: skip
    dark = (215, 340)
    assert sum(soft.getpixel(dark)) > sum(hard.getpixel(dark))  # shadow not burned out
    assert sum(soft.getpixel(dark)) <= sum(im.getpixel(dark))
    assert soft.getpixel((215, 230)) == (255, 255, 255)
    assert haze_stats(soft)["haze"] < haze_stats(im)["haze"]  # the veil still goes
    lut = postprocess.shadow_lut(0.4)
    assert lut[0] == 0 and lut[102] == 255
    assert all(b >= a for a, b in zip(lut, lut[1:], strict=False))


def test_light_frames_get_a_weaker_step():
    p = DehazeParams(light_luma=0.6, light_span=0.15, light_min=0.3, chroma=1.2)
    assert postprocess.light_factor(0.55, p) == 1.0
    assert postprocess.light_factor(0.675, p) == pytest.approx(0.65)
    assert postprocess.light_factor(0.9, p) == 0.3
    light = veiled_card(0.6)  # mean luma ≈ 0.8
    _, info = dehaze(light, p)
    _, plain = dehaze(light, DehazeParams(chroma=1.2))
    assert info["light_factor"] == 0.3 and info["luma"] > 0.75
    assert info["black"] == pytest.approx(plain["black"] * 0.3, abs=1e-3)
    assert info["chroma"] == pytest.approx(1.06)


def test_project_v2_presets_parse():
    cfg = postprocess.load_dehaze_config(ROOT)["presets"]
    for name in ("nano-v2", "gpt-v2"):
        p = preset_params(cfg[name], name)
        assert p.shadow_protect > 0 and p.light_luma < 1 and p.light_min < 1
