"""Runtime hard checks on the object mask (TA §11.2): safe zone, background simplicity, fill.

Approved by the user on 2026-11-05. Each check reads the same object mask as `crop`
(`object_mask.py`: rembg u2net from the local model, or `fake_masker` in fake runs) and the
safe zone of the frame (`knowledge/frames/safe_zone.json`). Thresholds live in
`config/mask_checks.toml`; they were calibrated offline on the 130 reference cards with
reviewed masks and checked against blind human labels of generated cards
(`scripts/stage04_mask_checks_eval.py`).

- **safe_zone** — share of the object mask inside the UI bands (stars on top, plaque at the
  bottom, side margins). The edge cut itself stays the separate check `crop`.
- **background_simplicity** (categories 1-2, TA §11.2) — outside the eroded mask: Canny edge
  density and the number of RGB colour clusters with >= 1 % of the background.
- **fill** — mask area and bbox area as fractions of the frame, both two-sided.

Unknown is never a pass: without a mask the checks are `not_run` (same as `crop`); a mask
that cannot be trusted (empty, almost the whole frame, shattered into pieces) or a missing
threshold gives `passed = None` with `required = True`, so the card goes to review.
"""

from __future__ import annotations

import functools
import json
import math
import tomllib
from pathlib import Path

from PIL import Image

CONFIG = "config/mask_checks.toml"
SAFE_ZONE = "knowledge/frames/safe_zone.json"
SAFE_ZONE_V2 = "knowledge/frames/safe_zone.v2.json"
CHECKS = ("safe_zone", "background_simplicity", "fill")
SYNTHETIC = "cardforge.synthetic_mask"  # PIL info key set by object_mask.fake_masker
# Reason codes of prompts/render/fixes.toml: a failure needs no judge to be fixed.
# `fill`: too large → `crop` (zoom out); too small has no code of the library (its codes are
# the blind-review reasons), so the fix is the English line FILL_SMALL.
CODES = {"safe_zone": "crop", "background_simplicity": "background", "fill": "crop"}
FILL_SMALL = (
    "Zoom in so the main object is larger and fills about half of the picture, "
    "still fully inside the safe zone with a clear margin."
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = (
            _merge(out[key], value)
            if isinstance(value, dict) and isinstance(out.get(key), dict)
            else value
        )
    return out


def load_config(root: Path | None = None, geometry: str | None = None) -> dict | None:
    """`config/mask_checks.toml` plus the safe zone; None when either file is missing.

    `frame_geometry` (or `geometry`) picks the safe zone: "v1" (default) — the bands of
    `safe_zone.json`; "v2" — the real frame occlusion of `safe_zone.v2.json` (2026-11-09),
    with the `[geometry_v2.*]` tables of the config laid over the v1 limits."""
    base = root or _PACKAGE_ROOT
    cfg = base / CONFIG
    if not cfg.is_file():
        return None
    out = tomllib.loads(cfg.read_text())
    chosen = geometry or out.get("frame_geometry", "v1")
    zone = base / (SAFE_ZONE_V2 if chosen == "v2" else SAFE_ZONE)
    if not zone.is_file():
        return None
    if chosen == "v2":
        out = _merge(out, out.get("geometry_v2", {}))
        out["version"] = out.get("geometry_v2", {}).get("version", out["version"] + "+v2")
    out["frame_geometry"] = chosen
    out["zone"] = json.loads(zone.read_text())
    out["zone"]["_root"] = str(base)
    return out


@functools.lru_cache(maxsize=4)
def _occlusion(path: str):
    import numpy as np

    return np.asarray(Image.open(path).convert("L")) >= 128


def bands(h: int, w: int, zone: dict):
    """Boolean arrays (top, bottom, side) of the UI bands (v1) or of the real frame
    occlusion (v2: stars and rounded top corners as `top`; nothing at the bottom or sides)."""
    import numpy as np

    if "occlusion_png" in zone:
        occ = _occlusion(str(Path(zone.get("_root", _PACKAGE_ROOT)) / zone["occlusion_png"]))
        if occ.shape != (h, w):
            raise ValueError("occlusion map and image dimensions differ")
        empty = np.zeros((h, w), bool)
        return occ, empty, empty.copy()

    top = np.zeros((h, w), bool)
    top[: int(np.ceil(h * zone["top_pct"]))] = True
    bottom = np.zeros((h, w), bool)
    bottom[int(np.floor(h * (1 - zone["bottom_pct"]))) :] = True
    side = np.zeros((h, w), bool)
    margin = int(np.ceil(w * zone["side_pct"]))
    side[:, :margin] = True
    side[:, w - margin :] = True
    return top, bottom, side


def measure(image: Image.Image, mask: Image.Image, zone: dict) -> dict:
    """Mask features of one card. Raises ValueError on a size mismatch or an empty mask.

    The keys `bbox_fill`, `mask_area`, `safe_overlap`, `background_edges` and
    `background_clusters` keep the stage-2 definitions of `hard_checks.features`.
    """
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"))
    raw = np.asarray(mask.convert("L"))
    if raw.shape != rgb.shape[:2]:
        raise ValueError("mask/image dimensions differ")
    m = raw >= 128
    h, w = m.shape
    y, x = np.where(m)
    if not len(x):
        raise ValueError("empty foreground mask")
    top, bottom, side = bands(h, w, zone)
    union = top | bottom | side
    total = m.sum()
    # Exclude a two-pixel boundary so foreground edges are not background complexity.
    bg = cv2.erode((~m).astype("uint8"), np.ones((5, 5), "uint8")) > 0
    edges = cv2.Canny(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), 100, 200) > 0
    bins = rgb[bg] // 32
    _, counts = np.unique(bins, axis=0, return_counts=True)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m.astype("uint8"), connectivity=8)
    largest = int(stats[1:, cv2.CC_STAT_AREA].max()) if n > 1 else 0
    return {
        "bbox_fill": float((x.max() - x.min() + 1) * (y.max() - y.min() + 1) / (w * h)),
        "mask_area": float(m.mean()),
        "safe_overlap": float((m & union).sum() / total),
        "overlap_top": float((m & top).sum() / total),
        "overlap_bottom": float((m & bottom).sum() / total),
        "overlap_side": float((m & side).sum() / total),
        "background_edges": float(edges[bg].mean()) if bg.any() else None,
        "background_clusters": int((counts / counts.sum() >= 0.01).sum()) if len(counts) else None,
        "background_share": float(bg.mean()),
        "largest_component": float(largest / total),
        "bbox": [
            round(float(x.min() / w), 4),
            round(float(y.min() / h), 4),
            round(float((x.max() + 1) / w), 4),
            round(float((y.max() + 1) / h), 4),
        ],
    }


def trust(features: dict, limits: dict) -> str | None:
    """Why the mask cannot be trusted, or None. Untrusted masks never pass (review)."""
    if features["mask_area"] < limits["min_area"]:
        return f"mask covers {features['mask_area']:.3f} of the frame (< {limits['min_area']})"
    if features["mask_area"] > limits["max_area"]:
        return f"mask covers {features['mask_area']:.3f} of the frame (> {limits['max_area']})"
    if features["largest_component"] < limits["min_largest_component"]:
        return (
            f"largest mask component is {features['largest_component']:.2f} of the mask "
            f"(< {limits['min_largest_component']})"
        )
    return None


def _limits(config: dict, check: str, category: int) -> dict | None:
    """The `[<check>.<group>]` table whose `categories` include this category."""
    for group in config.get(check, {}).values():
        if isinstance(group, dict) and category in group.get("categories", []):
            return group
    return None


def _compare(values: dict, limits: dict) -> tuple[list[str], dict]:
    """Limits are `<feature>_max` / `<feature>_min`; returns failed limits and thresholds."""
    failed, thresholds = [], {}
    for key, limit in limits.items():
        name, _, side = key.rpartition("_")
        if side not in ("min", "max") or name not in values:
            continue
        value = values[name]
        thresholds[key] = f"{'>=' if side == 'min' else '<='} {limit}"
        if value is None or not math.isfinite(value):
            failed.append(f"{name}:unknown")
        elif (side == "min" and value < limit) or (side == "max" and value > limit):
            failed.append(key)
    return failed, thresholds


def evaluate(features: dict | None, category: int, config: dict | None, why: str = "") -> dict:
    """The three checks for one card in the `HardResult.checks` format."""
    version = (config or {}).get("version", "missing")
    out = {}
    for check in CHECKS:
        base = {"code": CODES[check], "config": version}
        if check == "background_simplicity" and config and not _limits(config, check, category):
            out[check] = {
                **base,
                "passed": None,
                "required": False,
                "value": "not_applicable",
                "threshold": "categories 1-2 only (TA §11.2)",
            }
            continue
        if features is None:
            out[check] = {
                **base,
                "passed": None,
                "required": False,
                "value": "not_run",
                "threshold": CONFIG,
                "note": why or "no object mask",
            }
            continue
        if config is None:
            out[check] = {
                **base,
                "passed": None,
                "required": True,
                "value": "no config",
                "threshold": CONFIG,
                "note": f"{CONFIG} or {SAFE_ZONE} missing",
            }
            continue
        untrusted = trust(features, config["mask"])
        limits = _limits(config, check, category)
        if untrusted or not limits:
            out[check] = {
                **base,
                "passed": None,
                "required": True,
                "value": "untrusted mask" if untrusted else "no threshold",
                "threshold": CONFIG,
                "note": untrusted or f"no [{check}] limits for category {category}",
            }
            continue
        failed, thresholds = _compare(features, limits)
        unknown = [f for f in failed if f.endswith(":unknown")]
        value = {k: features[k] for k in {k.rpartition("_")[0] for k in thresholds}}
        out[check] = {
            **base,
            "passed": None if unknown else not failed,
            "required": True,
            "value": {k: round(v, 5) if isinstance(v, float) else v for k, v in value.items()},
            "threshold": thresholds,
            "failed": failed,
        }
        if check == "fill" and failed and all(f.endswith("_min") for f in failed):
            out[check]["code"] = None
            out[check]["fix_text"] = FILL_SMALL
    return out


def synthetic_backdrop(mask: Image.Image) -> Image.Image | None:
    """`fake_masker` masks are drawn without looking at the placeholder image, so the
    background of a fake run is measured on a flat backdrop (or a noisy one for attempts
    listed in the fake option `busy_background`). Real masks return None."""
    kind = mask.info.get(SYNTHETIC)
    if not kind:
        return None
    if kind == "busy":
        return Image.effect_noise(mask.size, 128).convert("RGB")
    return Image.new("RGB", mask.size, (200, 180, 160))


def mask_checks(
    image: Path, mask: Image.Image | None, category: int, config: dict | None = None,
    root: Path | None = None,
) -> dict:  # fmt: skip
    """Measure and evaluate; `config=None` loads `config/mask_checks.toml`."""
    config = config if config is not None else load_config(root)
    if mask is None:
        return evaluate(None, category, config, "no object mask (rembg or its model missing)")
    if config is None:
        return evaluate({}, category, None)
    try:
        with Image.open(image) as im:
            features = measure(synthetic_backdrop(mask) or im, mask, config["zone"])
    except ValueError as exc:
        empty = {"mask_area": 0.0, "largest_component": 0.0}
        result = evaluate(empty, category, config)
        for check in result.values():
            if check["passed"] is None:
                check["note"] = str(exc)
        return result
    return evaluate(features, category, config)
