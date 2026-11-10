"""Object mask and the runtime crop check (stage 4, phase 1 of the judge autonomy strategy).

The check answers one question: does the main object touch or run out of the frame?
GPT Image 2.5 often cuts the object at the top edge; raters marked "crop" when the object
touched or left the frame on any side (blind package kino-01, 91 cards, 3 raters).

Mask: rembg u2net on CPU from the local model `models/rembg/models/u2net/u2net.onnx`
(the `analysis` extra). Nothing is downloaded: without rembg or the model file the
check is `not_run` and never passes. Fake runs use a synthetic mask (`fake_masker`).

Thresholds (fraction of the edge covered by the mask within `EDGE_PX` pixels of a 430x480
card), measured on 2026-11-04 (`scripts/stage04_crop_check_eval.py`):
- categories 1-3: top >= 5 %, bottom >= 2 % or a side (upper 60 % of the height) >= 2 %;
- category 4 (scenes reach the frame by design): top >= 10 % only.
On the 130 reference cards of categories 1-4 these limits flag 4 (96.9 % pass); on kino-01
they flag 16 of 91 cards, 14 of them with a "crop" majority of the raters (precision 0.875,
recall 0.737 of 19) and every one with at least one "crop" vote. The second check asked
for (a second support line making three bands) was not built: too few labelled cases to
measure it (see the script's `support_bands`); the inspector question of code `support`
covers it instead.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw

EDGE_PX = 3
UPPER_SHARE = 0.6
# Defaults (phase 1) when config/mask_checks.toml has no `[crop.*]` group for the category.
# The config overrides them: from 2026-11-08 (user, variant A) categories 2-3 count a bottom
# contact only from 10 % of the width (`[crop.standing]`).
LIMITS = {
    "object": {"top": 0.05, "bottom": 0.02, "side": 0.02},  # categories 1-3
    "scene": {"top": 0.10},  # category 4
}
EDGES = ("top", "bottom", "side")
MODEL = "models/rembg/models/u2net/u2net.onnx"

Masker = Callable[[Path], Image.Image | None]


def _covered(strip: Image.Image, horizontal: bool) -> float:
    """Share of columns (horizontal strip) or rows (vertical strip) with any mask pixel."""
    w, h = strip.size
    line = strip.resize((w, 1) if horizontal else (1, h), Image.Resampling.BOX)
    values = line.tobytes()  # mode L: one byte per pixel
    return round(sum(v > 0 for v in values) / len(values), 4) if values else 0.0


def edge_contact(mask: Image.Image) -> dict[str, float] | None:
    m = mask.convert("L").point(lambda v: 255 if v >= 128 else 0)
    if m.getbbox() is None:
        return None
    w, h = m.size
    upper = max(EDGE_PX, int(h * UPPER_SHARE))
    left = _covered(m.crop((0, 0, EDGE_PX, upper)), horizontal=False)
    right = _covered(m.crop((w - EDGE_PX, 0, w, upper)), horizontal=False)
    return {
        "top": _covered(m.crop((0, 0, w, EDGE_PX)), horizontal=True),
        "bottom": _covered(m.crop((0, h - EDGE_PX, w, h)), horizontal=True),
        "side": max(left, right),
    }


def crop_check(
    mask: Image.Image | None,
    category: int,
    source: str = "rembg-u2net",
    frame: dict | None = None,
    edges: dict | None = None,
) -> dict:
    """Hard check `crop`; a failure carries the reason code `crop` (fix library).

    Two parts, both configured in config/mask_checks.toml (`None` reads the file):
    - edge contact: `[crop.*]` groups (`edges`); a category without a group, or `edges={}`,
      uses the phase-1 defaults `LIMITS`;
    - top headroom (2026-11-06): the soft mask must not reach closer to the top edge than
      `[frame.*] top_soft_min` (`frame={}` turns it off). A spire or cone in the star band up
      to the edge is a top crop for the user even without edge contact.

    `[crop] flag_edges` (user 2026-11-09, «Да, все три»): only these edges make the check
    fail (top touch and `top_headroom`); the other measured edges stay in `measured_edges`
    with `unflagged_would: "fix"` so the check can still be scored, but they do not flag."""
    if frame is None or edges is None:
        from cardforge.evaluate.mask_checks import load_config

        config = load_config() or {}
        frame = config.get("frame", {}) if frame is None else frame
        edges = config.get("crop", {}) if edges is None else edges
    configured = _frame_group(edges, category)
    limits = {k: configured[k] for k in EDGES if k in configured} or LIMITS[
        "scene" if category >= 4 else "object"
    ]
    group = _frame_group(frame, category)
    threshold = {k: f">= {v}" for k, v in limits.items()}
    if "top_soft_min" in group:
        threshold["top_headroom"] = f">= {group['top_soft_min']} (soft mask >= {SOFT})"
    base = {"required": False, "threshold": threshold, "code": "crop", "mask": source}
    if mask is None:
        return {**base, "passed": None, "value": "not_run",
                "note": "no object mask (rembg or its local model is unavailable)"}  # fmt: skip
    contact = edge_contact(mask)
    if contact is None:
        return {**base, "passed": None, "value": "empty mask", "note": "mask found no object"}
    over = [side for side, limit in limits.items() if contact[side] >= limit]
    margins = frame_margins(mask)
    if margins and "top_width_min" in group:
        margins = {**margins, **top_protrusion(mask)}
    if "top" in frame_flags(margins, category, frame):
        over.append("top_headroom")
    value = {**contact, "top_headroom": margins["top_soft"]} if margins else contact
    if margins and "top_width" in margins:
        value["top_width"] = margins["top_width"]
    allowed = edges.get("flag_edges")
    if allowed is None:
        return {**base, "passed": not over, "value": value, "edges": over}
    flagged = [e for e in over if e in allowed]
    out = {**base, "passed": not flagged, "value": value, "edges": flagged,
           "measured_edges": over, "flag_edges": list(allowed)}  # fmt: skip
    if over and not flagged:
        out["unflagged_would"] = "fix"
    return out


SOFT = 32  # soft mask level: thin or semi-transparent tops (spires, cones) of u2net masks
# A row counts as object only with this many mask pixels: no stray specks, and the thin
# strings of hanging objects (3-5 px) do not count as the object reaching the top.
MIN_ROW_PX = 8


def frame_margins(mask: Image.Image) -> dict[str, float] | None:
    """Free space above and below the object as fractions of the height, for the hard mask
    (>= 128) and the soft one (>= SOFT). None for an empty mask."""
    import numpy as np

    raw = np.asarray(mask.convert("L"))
    h = raw.shape[0]
    out = {}
    for name, level in (("", 128), ("_soft", SOFT)):
        rows = np.where((raw >= level).sum(axis=1) >= MIN_ROW_PX)[0]
        if not len(rows):
            return None
        out["top" + name] = round(float(rows[0] / h), 4)
        out["bottom" + name] = round(float((h - 1 - rows[-1]) / h), 4)
    return out


TOP_BAND = 0.025  # the top band of `top_protrusion`, as a share of the height


def top_protrusion(mask: Image.Image, band: float = TOP_BAND) -> dict[str, float]:
    """How much of the soft silhouette (>= SOFT) is in the top `band` of the frame: the share of
    the silhouette width (columns) and of its area. A thin antenna or flag near the edge is
    narrow and small; a cut roof or dome is wide (user 2026-11-09: small parts are fine)."""
    import numpy as np

    soft = np.asarray(mask.convert("L")) >= SOFT
    if not soft.any():
        return {"top_width": 0.0, "top_area": 0.0}
    h = soft.shape[0]
    cols = soft.any(axis=0)
    xs = np.where(cols)[0]
    rows = int(round(h * band))
    top = soft[:rows]
    return {
        "top_width": round(float(top.any(axis=0).sum() / (xs.max() - xs.min() + 1)), 4),
        "top_area": round(float(top.sum() / soft.sum()), 4),
    }


def load_frame_limits(root: Path | None = None) -> dict:
    """`[frame.*]` groups of config/mask_checks.toml (top crop, bottom crop, empty bottom)."""
    from cardforge.evaluate.mask_checks import load_config

    return (load_config(root) or {}).get("frame", {})


def _frame_group(limits: dict, category: int) -> dict:
    for group in limits.values():
        if isinstance(group, dict) and category in group.get("categories", []):
            return group
    return {}


def frame_flags(margins: dict | None, category: int, limits: dict) -> list[str]:
    """Which frame limits the margins break: `top` (object runs into the star band up to the
    edge), `bottom` (cut at the bottom), `empty_bottom` (too much empty space below)."""
    if not margins:
        return []
    g = _frame_group(limits, category)
    flags = []
    # `top_width_min` (v2 proposal, user 2026-11-09): a small part near the top edge (antenna,
    # flag, handle) narrower than this share of the silhouette width is not a top crop.
    small = "top_width_min" in g and margins.get("top_width", 1.0) < g["top_width_min"]
    if "top_soft_min" in g and margins["top_soft"] < g["top_soft_min"] and not small:
        flags.append("top")
    if "bottom_soft_min" in g and margins["bottom_soft"] < g["bottom_soft_min"]:
        flags.append("bottom")
    if "bottom_max" in g and margins["bottom"] > g["bottom_max"]:
        flags.append("empty_bottom")
    return flags


def rembg_masker(root: Path) -> Masker | None:
    """u2net masks from the local model only; None when rembg or the model is missing."""
    model = root / MODEL
    if not model.is_file():
        return None
    try:
        import rembg  # noqa: F401  (optional `analysis` extra)
    except ImportError:
        return None
    state: dict = {}
    lock = threading.Lock()

    def mask(path: Path) -> Image.Image | None:
        with lock:
            if "session" not in state:
                os.environ.setdefault("U2NET_HOME", str(root / "models/rembg"))
                os.environ.setdefault("NUMBA_CACHE_DIR", str(root / ".uv-cache/numba"))
                from rembg import new_session

                state["session"] = new_session("u2net", providers=["CPUExecutionProvider"])
            from rembg import remove

            with Image.open(path) as im:
                return remove(im.convert("RGB"), session=state["session"], only_mask=True)

    return mask


def fake_masker(options: dict | None = None) -> Masker:
    """Synthetic masks for FakeBackend runs: a centred object, or one cut at the top for
    `crop = {card_id: [attempt numbers]}`. The path is `cards/<v>/<card>/attempt_NN/...`.

    The mask is not drawn from the placeholder image, so it is marked synthetic: the mask
    checks measure its background on a flat backdrop, or on noise for the attempts in
    `busy_background = {card_id: [attempt numbers]}` (`mask_checks.synthetic_backdrop`)."""
    from cardforge.evaluate.mask_checks import SYNTHETIC

    opts = options or {}
    crop = {k: set(v) for k, v in opts.get("crop", {}).items()}
    busy = {k: set(v) for k, v in opts.get("busy_background", {}).items()}

    def mask(path: Path) -> Image.Image:
        folder = path.parent.parent if path.parent.name.startswith("cand_") else path.parent
        card_id = folder.parent.name
        attempt = int(folder.name.split("_")[-1]) if folder.name.startswith("attempt_") else 0
        with Image.open(path) as im:
            w, h = im.size
        out = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(out)
        if attempt in crop.get(card_id, set()):
            draw.ellipse([w * 0.25, -h * 0.2, w * 0.75, h * 0.6], fill=255)
        else:
            draw.ellipse([w * 0.25, h * 0.2, w * 0.75, h * 0.75], fill=255)
        out.info[SYNTHETIC] = "busy" if attempt in busy.get(card_id, set()) else "flat"
        return out

    return mask
