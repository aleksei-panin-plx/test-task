# ruff: noqa: E501
"""Explicit OCR adapters. A missing engine is unknown, never a clean image."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from cardforge.evaluate.common import sha256

ENGINES = ("auto", "apple-vision", "rapidocr")


def ocr_engine(root: Path) -> str:
    """`[ocr] engine` in config/cardforge.toml: `auto` (default) = Apple Vision on macOS, where the
    hard checks were calibrated, RapidOCR elsewhere (Windows, Linux; stage 7)."""
    import tomllib

    path = Path(root) / "config/cardforge.toml"
    cfg = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    engine = cfg.get("ocr", {}).get("engine", "auto")
    if engine not in ENGINES:
        raise ValueError(f"[ocr] engine must be one of {ENGINES}, got {engine!r}")
    if engine == "auto":
        return "apple-vision" if platform.system() == "Darwin" and shutil.which("swiftc") else "rapidocr"
    return engine


def recognize(paths: list[Path], root: Path, engine: str | None = None) -> dict:
    engine = engine or ocr_engine(root)
    if engine == "rapidocr":
        return recognize_rapid(paths)
    return recognize_apple(paths, root)


_RAPID = None


def recognize_rapid(paths: list[Path]) -> dict:
    """RapidOCR (PP-OCR on onnxruntime, models inside the wheel, CPU, no network). Same row format
    as Apple Vision: text, confidence, `bbox_bottom_left` normalised [x, y, w, h]."""
    global _RAPID
    try:
        import importlib.metadata

        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {str(p): {"status": "unavailable", "reason": "RapidOCR not installed"} for p in paths}
    if _RAPID is None:
        _RAPID = RapidOCR()
    version = importlib.metadata.version("rapidocr-onnxruntime")
    out = {}
    for p in paths:
        try:
            with Image.open(p) as im:
                width, height = im.size
            result, _ = _RAPID(str(p))
        except Exception as exc:  # an engine failure is unknown, never a clean image
            out[str(p)] = {"path": str(p), "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
            continue
        words = []
        for box, text, score in result or []:
            xs, ys = [pt[0] for pt in box], [pt[1] for pt in box]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            words.append({"text": text, "confidence": float(score),
                          "bbox_bottom_left": [x0 / width, 1 - y1 / height,
                                               (x1 - x0) / width, (y1 - y0) / height]})  # fmt: skip
        out[str(p)] = {"path": str(p), "status": "ok", "observations": words, "revision": version,
                       "engine": "RapidOCR", "os": platform.system(), "adapter_sha256": sha256(Path(__file__))}  # fmt: skip
    return out


def recognize_apple(paths: list[Path], root: Path) -> dict:
    if platform.system() != "Darwin" or not shutil.which("swiftc"):
        return {
            str(p): {"status": "unavailable", "reason": "Apple Vision unavailable"} for p in paths
        }
    source = Path(__file__).with_name("vision_ocr.swift")
    cache = root.resolve() / ".uv-cache/vision"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / ("ocr-" + sha256(source)[:16])
    env = {**os.environ, "CLANG_MODULE_CACHE_PATH": str(cache / "modules")}
    if not binary.exists():
        subprocess.run(
            [
                "swiftc",
                "-module-cache-path",
                str(cache / "modules"),
                str(source),
                "-o",
                str(binary),
            ],
            env=env,
            check=True,
            capture_output=True,
            timeout=120,
        )
    result = subprocess.run(
        [str(binary)],
        input=json.dumps([str(p) for p in paths]),
        text=True,
        capture_output=True,
        check=True,
        timeout=600,
    )
    rows = json.loads(result.stdout)
    return {
        r["path"]: {
            **r,
            "engine": "Apple Vision",
            "os": platform.mac_ver()[0],
            "adapter_sha256": sha256(source),
        }
        for r in rows
    }
