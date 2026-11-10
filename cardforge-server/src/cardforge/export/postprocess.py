"""Raw render → final 430×480 card art (TA §10.10).

1. Crop to the 430:480 aspect; the crop window moves so the estimated object box lands in
   the safe window, clamped to the image.
2. Lanczos down to 430×480. 3. sRGB RGB PNG. Provenance stays in `bundle.json`, not in PNG
   metadata. The object box comes from a border-colour difference heuristic (no rembg in
   the base install); it only positions the crop and is never a quality signal.
4. Optional «снять фильтр» (dehaze, E1 2026-11-06, `config/postprocess.toml`): off unless the
   render profile is listed there. See `dehaze` below.
"""

from __future__ import annotations

import io
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PIL import Image, ImageChops, ImageCms, ImageEnhance, ImageFilter, ImageStat

from cardforge.contracts.request import SafeZone
from cardforge.orchestrator.store import write_atomic_bytes

SIZE = (430, 480)
ASPECT = SIZE[0] / SIZE[1]


def estimate_object_box(
    image: Image.Image, threshold: int = 40
) -> tuple[int, int, int, int] | None:
    small = image.convert("RGB").resize((96, int(96 * image.height / image.width)))
    w, h = small.size
    border = [small.getpixel((x, 0)) for x in range(w)] + [
        small.getpixel((x, h - 1)) for x in range(w)
    ]
    median = tuple(sorted(p[i] for p in border)[len(border) // 2] for i in range(3))
    diff = ImageChops.difference(small, Image.new("RGB", small.size, median)).convert("L")
    mask = diff.point(lambda v: 255 if v > threshold else 0).filter(ImageFilter.MedianFilter(5))
    box = mask.getbbox()
    if box is None:
        return None
    sx, sy = image.width / w, image.height / h
    return (int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy))


def crop_window(width: int, height: int, box, zone: SafeZone) -> tuple[int, int, int, int]:
    if width / height > ASPECT:
        cw, ch = round(height * ASPECT), height
    else:
        cw, ch = width, round(width / ASPECT)
    left, top = (width - cw) // 2, (height - ch) // 2
    if box is not None:
        # Centre of the safe window inside the crop, in crop pixels.
        safe_cx = cw * 0.5
        safe_cy = ch * (zone.top_pct + (1 - zone.top_pct - zone.bottom_pct) / 2)
        obj_cx, obj_cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        left = round(obj_cx - safe_cx)
        top = round(obj_cy - safe_cy)
    left = min(max(0, left), width - cw)
    top = min(max(0, top), height - ch)
    return left, top, left + cw, top + ch


def finalize(
    raw_png: bytes, zone: SafeZone, dehaze_params: DehazeParams | None = None
) -> tuple[bytes, dict]:
    with Image.open(io.BytesIO(raw_png)) as im:
        image = im.convert("RGB")
    box = estimate_object_box(image)
    window = crop_window(image.width, image.height, box, zone)
    final = image.crop(window).resize(SIZE, Image.Resampling.LANCZOS)
    dehaze_info = None
    if dehaze_params is not None:
        final, dehaze_info = dehaze(final, dehaze_params)
    buf = io.BytesIO()
    final.save(buf, format="PNG")
    info = {
        "raw_size": [image.width, image.height],
        "object_box_estimate": list(box) if box else None,
        "crop": list(window),
        "size": list(SIZE),
        "mode": "RGB",
        "resample": "lanczos",
    }
    if dehaze_info is not None:
        info["dehaze"] = dehaze_info
    return buf.getvalue(), info


def write_final(
    raw_path: Path, final_path: Path, zone: SafeZone, dehaze_params: DehazeParams | None = None
) -> dict:
    data, info = finalize(raw_path.read_bytes(), zone, dehaze_params)
    write_atomic_bytes(final_path, data)
    return info


# ---------------------------------------------------------------- «снять фильтр» (dehaze)
#
# The user rejects many cards for «слишком блёклые цвета», «как будто наложили фильтр» on
# the whole picture. Measured on 430×480 finals (docs/research/generation-defects.md §2):
# haze = mean min(R,G,B) is 0.26 on the 160 references, 0.32 on GPT 2.5, 0.43 on Nano v2;
# p90 of HSV saturation is 0.95 / 0.88 / 0.70. The milky veil is modelled as I' = I(1-b) + b
# and removed by the inverse levels (I - b)/(1 - b): white stays white, the veil goes.
# b is adaptive — the share of the haze above `target_haze` times `strength`, capped at
# `max_black` — so an already clean image gets no levels change (chroma still applies).
# Below `toe`·b the curve is a quadratic toe that meets 0 at 0: darks deepen smoothly
# instead of a hard cut. Then chroma ×`chroma` around ITU-R 601 luma (Pillow
# ImageEnhance.Color; neutral whites and greys do not change).
# Background guard: with a rough object mask (border-colour difference, the same heuristic
# as the crop) the background is processed at 1, 0.5 or 0 strength — the largest whose
# object/background ΔE (CIELAB, means) does not fall more than `bg_guard_tolerance` below
# the original image's. The step fixes no material or highlight defects.

POSTPROCESS_CONFIG = "config/postprocess.toml"


@dataclass(frozen=True)
class DehazeParams:
    preset: str = "default"
    target_haze: float = 0.24
    strength: float = 1.0
    max_black: float = 0.25
    toe: float = 3.0
    chroma: float = 1.15
    bg_guard: bool = True
    bg_guard_tolerance: float = 1.0
    mask_threshold: int = 40
    # v2 (after the E1 labels): softer shadows and lighter frames. Defaults = off (v1).
    shadow_protect: float = 0.0  # levels fade in over luma 0..shadow_protect (smoothstep)
    light_luma: float = 1.0  # frame mean luma above which the step weakens
    light_span: float = 0.15  # ... linearly over this luma range ...
    light_min: float = 1.0  # ... down to this share of black and of chroma − 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.target_haze < 1.0:
            raise ValueError("dehaze target_haze must be in [0, 1)")
        if not 0.0 <= self.max_black < 0.5:
            raise ValueError("dehaze max_black must be in [0, 0.5)")
        if self.toe < 2.0:
            raise ValueError("dehaze toe must be >= 2 (monotone curve)")
        if not 1.0 <= self.chroma <= 1.5:
            raise ValueError("dehaze chroma must be in [1, 1.5]")
        if self.strength < 0:
            raise ValueError("dehaze strength must be >= 0")
        if not 0.0 <= self.shadow_protect <= 1.0:
            raise ValueError("dehaze shadow_protect must be in [0, 1]")
        if not 0.0 <= self.light_min <= 1.0 or self.light_span <= 0:
            raise ValueError("dehaze light_min must be in [0, 1] and light_span > 0")


def load_dehaze_config(root: Path) -> dict:
    path = Path(root) / POSTPROCESS_CONFIG
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("dehaze", {})


def dehaze_params_for(root: Path, profile_id: str | None) -> DehazeParams | None:
    """Params for this render profile, or None (the step is off unless listed)."""
    cfg = load_dehaze_config(root)
    preset = cfg.get("profiles", {}).get(profile_id) if profile_id else None
    if not preset:
        return None
    presets = cfg.get("presets", {})
    if preset not in presets:
        raise ValueError(
            f"dehaze preset {preset!r} for {profile_id!r} is not in {POSTPROCESS_CONFIG}"
        )
    return preset_params(presets[preset], preset)


def preset_params(data: dict, name: str) -> DehazeParams:
    names = {f.name for f in fields(DehazeParams)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown dehaze parameters {sorted(unknown)} in preset {name!r}")
    return DehazeParams(**{**data, "preset": name})


def haze_stats(image: Image.Image) -> dict:
    """haze = mean min(R,G,B); sat_p90 / sat_mean = HSV saturation over the frame (0..1)."""
    rgb = image.convert("RGB")
    r, g, b = rgb.split()
    lo = ImageChops.darker(r, ImageChops.darker(g, b))
    sat = rgb.convert("HSV").getchannel("S")
    hist = sat.histogram()
    total, acc, p90 = sum(hist), 0, 255
    for v, n in enumerate(hist):
        acc += n
        if acc >= 0.9 * total:
            p90 = v
            break
    return {
        "haze": round(ImageStat.Stat(lo).mean[0] / 255, 4),
        "sat_p90": round(p90 / 255, 4),
        "sat_mean": round(ImageStat.Stat(sat).mean[0] / 255, 4),
    }


def levels_lut(black: float, toe: float) -> list[int]:
    """Inverse veil (x - b)/(1 - b) with a quadratic toe below k = toe·b (f(0)=0, C1 at k)."""
    if black <= 0:
        return list(range(256))
    s = 1.0 / (1.0 - black)
    k = min(toe * black, 0.999)
    v = (k - black) * s
    a = (s * k - v) / (k * k)
    beta = s - 2 * a * k
    out = []
    for i in range(256):
        x = i / 255
        y = (x - black) * s if x >= k else a * x * x + beta * x
        out.append(max(0, min(255, round(y * 255))))
    return out


def object_mask(image: Image.Image, threshold: int = 40) -> Image.Image | None:
    """Rough object mask (L, 255 = object): border-colour difference, as for the crop."""
    rgb = image.convert("RGB")
    w, h = rgb.size
    border = [rgb.getpixel((x, 0)) for x in range(0, w, 2)] + [
        rgb.getpixel((x, h - 1)) for x in range(0, w, 2)
    ]
    median = tuple(sorted(p[i] for p in border)[len(border) // 2] for i in range(3))
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, median)).convert("L")
    mask = diff.point(lambda v: 255 if v > threshold else 0).filter(ImageFilter.MedianFilter(5))
    share = ImageStat.Stat(mask).mean[0] / 255
    if not 0.02 <= share <= 0.9:
        return None
    return mask


_LAB = ImageCms.buildTransform(
    ImageCms.createProfile("sRGB"), ImageCms.createProfile("LAB"), "RGB", "LAB"
)


def _lab_mean(image: Image.Image, mask: Image.Image) -> tuple[float, float, float]:
    lab = ImageCms.applyTransform(image, _LAB)
    m = ImageStat.Stat(lab, mask=mask).mean
    return (m[0] * 100 / 255, m[1] - 128, m[2] - 128)


def separation(image: Image.Image, mask: Image.Image) -> float:
    """ΔE76 between the mean CIELAB colour of the object and of the background."""
    obj = _lab_mean(image, mask)
    bg = _lab_mean(image, ImageChops.invert(mask))
    return sum((x - y) ** 2 for x, y in zip(obj, bg, strict=True)) ** 0.5


def shadow_lut(edge: float) -> list[int]:
    """Weight of the levels change by luma: smoothstep 0 → 1 over 0..edge (v2)."""
    out = []
    for i in range(256):
        t = min(1.0, i / 255 / edge)
        out.append(round(255 * t * t * (3 - 2 * t)))
    return out


def light_factor(luma: float, p: DehazeParams) -> float:
    """1 for ordinary frames, down to `light_min` for frames that are meant to be light."""
    if luma <= p.light_luma:
        return 1.0
    return max(p.light_min, 1.0 - (1.0 - p.light_min) * (luma - p.light_luma) / p.light_span)


def _apply(image: Image.Image, black: float, chroma: float, p: DehazeParams) -> Image.Image:
    out = image.point(levels_lut(black, p.toe) * 3) if black > 0 else image.copy()
    if black > 0 and p.shadow_protect > 0:
        # Darks keep (most of) their original value: the levels only act on mid and light
        # tones, so black shadows soften instead of burning out (user, E1 labels).
        weight = image.convert("L").point(shadow_lut(p.shadow_protect))
        out = Image.composite(out, image, weight)
    if chroma != 1.0:
        out = ImageEnhance.Color(out).enhance(chroma)
    return out


def dehaze(image: Image.Image, p: DehazeParams) -> tuple[Image.Image, dict]:
    rgb = image.convert("RGB")
    before = haze_stats(rgb)
    luma = round(ImageStat.Stat(rgb.convert("L")).mean[0] / 255, 4)
    light = round(light_factor(luma, p), 4)
    excess = max(0.0, before["haze"] - p.target_haze) / (1.0 - p.target_haze)
    black = round(min(p.max_black, p.strength * excess) * light, 4)
    chroma = round(1.0 + (p.chroma - 1.0) * light, 4)
    full = _apply(rgb, black, chroma, p)
    bg_strength, sep = 1.0, None
    mask = object_mask(rgb, p.mask_threshold) if p.bg_guard else None
    out = full
    if mask is not None:
        soft = mask.filter(ImageFilter.GaussianBlur(3))
        hard = mask.point(lambda v: 255 if v >= 128 else 0)
        sep = {"before": round(separation(rgb, hard), 2)}
        candidates = {}
        for t in (1.0, 0.5, 0.0):
            bg = full if t == 1.0 else Image.blend(rgb, full, t)
            candidates[t] = Image.composite(full, bg, soft)
            sep[str(t)] = round(separation(candidates[t], hard), 2)
        floor = sep["before"] - p.bg_guard_tolerance
        bg_strength = next((t for t in (1.0, 0.5) if sep[str(t)] >= floor), 0.0)
        out = candidates[bg_strength]
    return out, {
        "params": asdict(p),
        "black": black,
        "chroma": chroma,
        "luma": luma,
        "light_factor": light,
        "background_strength": bg_strength,
        "separation_delta_e": sep,
        "before": before,
        "after": haze_stats(out),
    }
