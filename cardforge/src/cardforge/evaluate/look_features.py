"""«Look» signals of one card: milky haze, pure saturated colour, small highlights (stage 4,
work 6 of `docs/research/generation-defects.md`, 2026-11-06). **Signal only**: the entry
`look` of `HardResult.checks` has `passed = None`, `required = False`, `signal_only = True`, so
it never changes a card decision and never sends a card to review. It is written with the other
checks into `<attempt>/eval.json` (`hard_checks.look`) and the `check_result` event.

Definitions are the ones of the offline analysis of blind-labels-02 (§2 of the research note),
measured on the 430×480 art (other sizes are resized first, so blob counts stay comparable):

- **haze** — mean of min(R, G, B) / 255 over the frame: a raised minimum channel is the milky
  veil («как будто наложили фильтр»). Medians: references 0.24, GPT 0.29–0.33, Nano 0.41–0.43.
- **sat_p90** — 90th percentile of HSV saturation over the frame: are there pure colours at
  all. References 0.95, GPT 0.85–0.89, Nano 0.69–0.75. `sat_mean` — the mean.
- **highlights** (on the object mask) — pixels with Lab L > 70 that are > 10 L brighter than a
  Gaussian blur (σ = 6) of L; 8-connected blobs of ≥ 4 px are counted. `count`, `area` (share of
  the mask), blob size in px (`size_mean`, `size_median`, `size_max`) and `sharpness` (mean
  Sobel gradient of L on the one-pixel ring around the blobs). References ≈ 37 blobs, GPT ≈ 50.
- phase-4 mask features (`runs/stage04/judge-research/phase4/style_features.py`): `mask_area`,
  `bg_L_std`, `img_colorfulness`, `dL_signed_bg`, and the proposed runtime flag
  `mask_area < 0.28 or bg_L_std > 12.7` (`phase4_flag`).

Every feature also gets `ref_pct`: its approximate percentile among the customer references
(quantiles in `config/look_features.toml`, made by `scripts/stage04_look_features_refs.py`).
No threshold here rejects anything; calibration against human reasons is a separate step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from PIL import Image

CONFIG = "config/look_features.toml"
VERSION = "look-features-v1"
SIZE = (430, 480)
SPEC_L_MIN, SPEC_EXCESS, SPEC_SIGMA, SPEC_MIN_PX = 70.0, 10.0, 6.0, 4
PHASE4_AREA, PHASE4_BG_STD = 0.28, 12.7
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


def load_config(root: Path | None = None) -> dict | None:
    path = (root or _PACKAGE_ROOT) / CONFIG
    return tomllib.loads(path.read_text()) if path.is_file() else None


def _colorfulness(rgb) -> float:
    """Hasler & Süsstrunk (2003) on N x 3 pixels, as in phase 4."""
    import numpy as np

    r, g, b = (rgb[:, i].astype("float64") for i in range(3))
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def measure(image: Image.Image, mask: Image.Image | None = None) -> dict:
    """Features of one card; object features are None without a usable mask."""
    import cv2
    import numpy as np

    im = image.convert("RGB")
    if im.size != SIZE:
        im = im.resize(SIZE, Image.Resampling.LANCZOS)
    rgb = np.asarray(im)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype("float32")
    L = lab[..., 0] * 100 / 255
    sat = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1].astype("float32") / 255
    dark = rgb.min(2).astype("float32") / 255
    out: dict = {
        "haze": float(dark.mean()),
        "sat_p90": float(np.percentile(sat, 90)),
        "sat_mean": float(sat.mean()),
        "img_colorfulness": _colorfulness(rgb.reshape(-1, 3)),
        "obj_haze": None,
        "mask_area": None,
        "bg_L_std": None,
        "dL_signed_bg": None,
        "highlights": None,
        "phase4_flag": None,
    }
    if mask is None:
        return out
    m = mask.convert("L")
    if m.size != SIZE:
        m = m.resize(SIZE, Image.Resampling.NEAREST)
    m = np.asarray(m) >= 128
    if m.sum() < 50:
        return out
    bg = cv2.erode((~m).astype("uint8"), np.ones((5, 5), "uint8")) > 0
    if not bg.any():
        bg = ~m
    blur = cv2.GaussianBlur(L, (0, 0), SPEC_SIGMA)
    spec = ((L - blur) > SPEC_EXCESS) & (L > SPEC_L_MIN) & m
    _, labels, stats, _ = cv2.connectedComponentsWithStats(spec.astype("uint8"), connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    kept = areas[areas >= SPEC_MIN_PX]
    gx, gy = cv2.Sobel(L, cv2.CV_32F, 1, 0), cv2.Sobel(L, cv2.CV_32F, 0, 1)
    ring = (cv2.dilate(spec.astype("uint8"), np.ones((3, 3), "uint8")) > 0) & ~spec
    area, bg_std = float(m.mean()), float(L[bg].std()) if bg.any() else None
    out.update(
        obj_haze=float(dark[m].mean()),
        mask_area=area,
        bg_L_std=bg_std,
        dL_signed_bg=float(L[m].mean() - L[bg].mean()) if bg.any() else None,
        highlights={
            "count": int(len(kept)),
            "area": float(spec.sum() / m.sum()),
            "size_mean": float(kept.mean()) if len(kept) else 0.0,
            "size_median": float(np.median(kept)) if len(kept) else 0.0,
            "size_max": int(kept.max()) if len(kept) else 0,
            "sharpness": float(np.hypot(gx, gy)[ring].mean()) if ring.any() else 0.0,
        },
        phase4_flag=bool(area < PHASE4_AREA or (bg_std is not None and bg_std > PHASE4_BG_STD)),
    )
    return out


def flat(features: dict) -> dict[str, float]:
    """Numeric features as `name` / `highlights.<name>` → value (None dropped)."""
    out = {}
    for k, v in features.items():
        if isinstance(v, dict):
            out.update({f"{k}.{kk}": vv for kk, vv in v.items() if vv is not None})
        elif isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = v
    return out


def ref_percentiles(features: dict, config: dict | None) -> dict[str, float]:
    """Approximate percentile of each feature among the references (linear between the
    stored quantiles, clipped to 0..100)."""
    import numpy as np

    quantiles = (config or {}).get("reference", {}).get("quantiles", {})
    out = {}
    for name, value in flat(features).items():
        q = quantiles.get(name)
        if not q:
            continue
        pts = sorted((float(v), float(p)) for p, v in q.items())
        xs, ps = [x for x, _ in pts], [p for _, p in pts]
        out[name] = round(float(np.interp(value, xs, ps, left=0.0, right=100.0)), 1)
    return out


def _round(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    return v


def look_signal(
    image: Path, mask: Image.Image | None, config: dict | None = None, root: Path | None = None
) -> dict:
    """The `look` entry of `HardResult.checks`: always signal-only, never fails the card."""
    config = config if config is not None else load_config(root)
    base = {
        "passed": None,
        "required": False,
        "signal_only": True,
        "code": None,
        "config": (config or {}).get("version", "missing"),
        "threshold": "none (signal only; docs/research/generation-defects.md §7.6)",
    }
    try:
        with Image.open(image) as im:
            features = measure(im, mask)
    except (OSError, ValueError) as exc:
        return {**base, "value": "not_run", "note": str(exc)[:200]}
    entry = {**base, "value": _round(features), "ref_pct": ref_percentiles(features, config)}
    if mask is None:
        entry["note"] = "no object mask: object features not measured"
    elif mask.info.get("cardforge.synthetic_mask"):
        entry["note"] = "synthetic mask of a fake run"
    return entry
