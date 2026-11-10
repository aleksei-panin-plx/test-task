"""Stage 7 (Windows, user 2026-11-10 «В этапе 7»): RapidOCR is the OCR outside macOS; same row
format as Apple Vision; `auto` keeps the calibrated Apple Vision on macOS."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from cardforge.evaluate import ocr

ROOT = Path(__file__).resolve().parents[1]


def _text_card(path: Path) -> Path:
    im = Image.new("RGB", (430, 480), "white")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default(size=48)
    ImageDraw.Draw(im).text((60, 200), "HELLO 2026", fill="black", font=font)
    im.save(path)
    return path


def test_auto_engine_by_platform(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/cardforge.toml").write_text('[ocr]\nengine = "auto"\n')
    monkeypatch.setattr(ocr.platform, "system", lambda: "Windows")
    assert ocr.ocr_engine(tmp_path) == "rapidocr"
    monkeypatch.setattr(ocr.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/swiftc")
    assert ocr.ocr_engine(tmp_path) == "apple-vision"
    (tmp_path / "config/cardforge.toml").write_text('[ocr]\nengine = "rapidocr"\n')
    assert ocr.ocr_engine(tmp_path) == "rapidocr"
    (tmp_path / "config/cardforge.toml").write_text('[ocr]\nengine = "tesseract"\n')
    with pytest.raises(ValueError, match="engine"):
        ocr.ocr_engine(tmp_path)


def test_rapidocr_reads_text_in_the_apple_format(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    card = _text_card(tmp_path / "t.png")
    blank = tmp_path / "b.png"
    Image.new("RGB", (430, 480), (120, 180, 230)).save(blank)
    rows = ocr.recognize([card, blank], ROOT, "rapidocr")
    row = rows[str(card)]
    assert row["status"] == "ok" and row["engine"] == "RapidOCR"
    words = row["observations"]
    assert "".join(w["text"] for w in words).replace(" ", "") == "HELLO2026"
    x, y, w, h = words[0]["bbox_bottom_left"]
    assert 0 < x < 0.5 and 0.3 < y < 0.7 and 0 < w <= 1 and 0 < h < 0.3
    assert rows[str(blank)] == {**rows[str(blank)], "status": "ok", "observations": []}


def test_missing_rapidocr_is_unknown_not_clean(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_rapid(name, *a, **kw):
        if name.startswith("rapidocr"):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_rapid)
    out = ocr.recognize_rapid([tmp_path / "x.png"])
    assert out[str(tmp_path / "x.png")]["status"] == "unavailable"
