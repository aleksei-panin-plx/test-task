"""Set QA (TA §11.6): deterministic checks over the plan and the final images.

Three set-level signals, none of which rejects a card on its own:

- deterministic plan checks (categories, colour rhythm, spread) and dHash near-duplicates;
- style consistency from style embeddings (CSD primary, DINOv2 auxiliary, both local CPU):
  the set's dispersion against the real catalog sets (TA: «дисперсия эмбеддингов сета не больше
  95-го перцентиля дисперсии реальных сетов») and, per card, its leave-one-out distance to the
  other cards of the set, against the same distances inside real sets;
- the VLM review of the 5×2 sheet (`cardforge.evaluate.sheet_review`), passed in as a dict.

Thresholds live in `config/runtime.toml` `[set_qa]`; an outlier makes the set `warn`
(`action = "warn"`), never an automatic rejection of the card.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from itertools import combinations
from pathlib import Path

from PIL import Image

from cardforge.contracts.generation import SetEvaluation
from cardforge.contracts.request import SetPlan
from cardforge.planner.validators import object_count_rules

DHASH_DUPLICATE_BITS = 6  # of 64; near-identical images only

STYLE_INDEXES = {
    "csd": ("dataset/analysis/evaluation/csd-style-index.json", "models/csd-vit-l"),
    "dinov2": ("dataset/analysis/evaluation/style-index.json", "models/dinov2-base"),
}
DEFAULT_STYLE = {
    "metric": "csd",
    "min_cards": 4,
    "outlier_percentile": 95.0,
    "outlier_robust_z": 2.0,
    "dispersion_percentile": 95.0,
}

Embedder = Callable[[list[Path]], list[list[float]]]


def dhash(path: Path) -> int:
    with Image.open(path) as im:
        small = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


# ---------------------------------------------------------------- style consistency


def _unit(matrix):
    import numpy as np

    x = np.asarray(matrix, dtype=float)
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError("expected a finite embedding matrix")
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("zero embedding")
    return x / norms


def _loo_distances(x) -> list[float]:
    """1 − cos(card, centroid of the other cards of the same set)."""
    import numpy as np

    others = _unit(x.sum(axis=0, keepdims=True) - x)
    return np.clip(1 - (x * others).sum(axis=1), 0, 2).tolist()


def _dispersion(x) -> float:
    """Mean 1 − cos to the set centroid (TA §11.6 «дисперсия эмбеддингов сета»)."""
    import numpy as np

    centroid = _unit(x.mean(axis=0, keepdims=True))[0]
    return float(np.clip(1 - x @ centroid, 0, 2).mean())


def _percentile_of(value: float, distribution: Sequence[float]) -> float:
    below = sum(d < value for d in distribution)
    equal = sum(d == value for d in distribution)
    return 100.0 * (below + 0.5 * equal) / len(distribution)


def _quantile(values: Sequence[float], q: float) -> float:
    import numpy as np

    return float(np.percentile(np.asarray(values, dtype=float), q))


def reference_sets(index: dict, set_of: dict[str, int], min_cards: int = 4) -> dict:
    """Distributions of the real catalog sets of the same index: set dispersion and the
    per-card leave-one-out distance to its set-mates. Sets smaller than `min_cards` are
    skipped (LOO needs set-mates)."""

    x = _unit(index["vectors"])
    groups: dict[int, list[int]] = {}
    for i, ref in enumerate(index["ids"]):
        if ref in set_of:
            groups.setdefault(set_of[ref], []).append(i)
    dispersions, loo, sizes = [], [], []
    for _, rows in sorted(groups.items()):
        if len(rows) < min_cards:
            continue
        xs = x[rows]
        dispersions.append(_dispersion(xs))
        loo.extend(_loo_distances(xs))
        sizes.append(len(rows))
    if len(dispersions) < 5:
        raise ValueError("need at least five real sets for set-level style statistics")
    return {
        "sets": len(dispersions),
        "set_sizes": sizes,
        "dispersions": dispersions,
        "loo": loo,
        "centroid": _unit(x.mean(axis=0, keepdims=True))[0].tolist(),
        "reference_centroid_distances": index["reference_centroid_distances_loo"],
        "model": index.get("model"),
    }


def style_consistency(vectors: dict[str, Sequence[float]], ref: dict, cfg: dict | None = None):
    """Set-level style statistics for the accepted cards of one variant.

    A card is an outlier when its LOO distance to the other cards is at or above
    `outlier_percentile` of the same distances inside real sets AND its robust z-score
    within this set (median/MAD) is at least `outlier_robust_z`: far in absolute terms
    and far from the rest of this set. Distance to the reference centroid is reported for
    context only (generated sets differ from the catalog as a whole)."""
    import numpy as np

    cfg = {**DEFAULT_STYLE, **(cfg or {})}
    ids = list(vectors)
    if len(ids) < int(cfg["min_cards"]):
        return {
            "status": "not_run",
            "reason": f"{len(ids)} accepted cards < min_cards {cfg['min_cards']}",
        }
    x = _unit([vectors[i] for i in ids])
    loo = _loo_distances(x)
    median = float(np.median(loo))
    mad = float(np.median(np.abs(np.asarray(loo) - median))) * 1.4826
    # Floor the spread: in a very tight set a tiny absolute gap must not become a huge z.
    spread = max(mad, 0.05 * median, 1e-6)
    centroid = np.asarray(ref["centroid"])
    cards = {}
    outliers = []
    card_threshold = _quantile(ref["loo"], float(cfg["outlier_percentile"]))
    for cid, d, v in zip(ids, loo, x, strict=True):
        z = (d - median) / spread
        ref_d = float(np.clip(1 - v @ centroid, 0, 2))
        flagged = d >= card_threshold and z >= float(cfg["outlier_robust_z"])
        cards[cid] = {
            "loo_distance": round(d, 5),
            "loo_percentile_real_sets": round(_percentile_of(d, ref["loo"]), 1),
            "robust_z": round(z, 2),
            "reference_centroid_distance": round(ref_d, 5),
            "reference_centroid_percentile": round(
                _percentile_of(ref_d, ref["reference_centroid_distances"]), 1
            ),
            "outlier": flagged,
        }
        if flagged:
            outliers.append(cid)
    dispersion = _dispersion(x)
    dispersion_threshold = _quantile(ref["dispersions"], float(cfg["dispersion_percentile"]))
    return {
        "status": "ok",
        "metric": cfg["metric"],
        "n": len(ids),
        "dispersion": round(dispersion, 5),
        "dispersion_percentile_real_sets": round(_percentile_of(dispersion, ref["dispersions"]), 1),
        "dispersion_threshold": round(dispersion_threshold, 5),
        "dispersion_ok": dispersion <= dispersion_threshold,
        "card_threshold": round(card_threshold, 5),
        "outliers": outliers,
        "cards": cards,
        "thresholds": {k: cfg[k] for k in DEFAULT_STYLE},
        "real_sets": ref["sets"],
    }


def load_reference(root: Path, metric: str, min_cards: int = 4) -> dict:
    """The stored reference index of `metric` (no model is loaded), checked against the
    local model manifest so a changed model never meets an old index."""
    from cardforge.knowledge.catalog import load_cards

    if metric not in STYLE_INDEXES:
        raise ValueError(f"unknown style metric {metric!r}; choose csd or dinov2")
    index_rel, model_rel = STYLE_INDEXES[metric]
    index = json.loads((root / index_rel).read_text())
    source = json.loads((root / model_rel / "source.json").read_text())
    if metric == "csd" and index["model"] != source:
        raise ValueError("CSD index and model differ")
    if metric == "dinov2" and index["model"]["revision"] != source["revision"]:
        raise ValueError("DINOv2 index and model differ")
    cards = load_cards(root)
    hashes = {c.card_ref: c.source_sha256 for c in cards}
    for ref, digest in zip(index["ids"], index["hashes"], strict=True):
        if hashes.get(ref) != digest:
            raise ValueError("stale reference style embedding")
    return reference_sets(index, {c.card_ref: c.set_index for c in cards}, min_cards)


def local_embedder(root: Path, metric: str) -> Embedder:
    """CPU embedder of `metric` from the already downloaded weights (no download)."""
    _, model_rel = STYLE_INDEXES[metric]
    folder = root / model_rel
    if metric == "csd":
        from cardforge.evaluate.csd_metric import embed as csd_embed

        return lambda paths: csd_embed(paths, folder)
    from cardforge.evaluate.style_metric import embed as dino_embed

    return lambda paths: dino_embed(paths, folder)


def _style_notes(style: dict | None) -> list[str]:
    if style is None:
        return ["style consistency: not run (no style embedder in this runtime)"]
    if style.get("status") != "ok":
        return [f"style consistency: not run ({style.get('reason')})"]
    note = (
        f"style consistency ({style['metric']}): dispersion {style['dispersion']:.4f} = "
        f"p{style['dispersion_percentile_real_sets']:.0f} of {style['real_sets']} real sets "
        f"(limit {style['dispersion_threshold']:.4f})"
    )
    notes = [note]
    if not style["dispersion_ok"]:
        notes.append("style dispersion above the real-set limit")
    for cid in style["outliers"]:
        c = style["cards"][cid]
        notes.append(
            f"style outlier {cid}: LOO {c['loo_distance']:.4f} "
            f"(p{c['loo_percentile_real_sets']:.0f} of real sets, z {c['robust_z']:.1f})"
        )
    return notes


def _sheet_notes(sheet: dict | None) -> list[str]:
    if sheet is None:
        return []
    if sheet.get("status") != "ok":
        return [f"sheet review: not run ({sheet.get('reason')})"]
    head = (
        f"sheet review ({sheet['model']}): "
        + ("consistent" if sheet["consistent_set"] else "inconsistent")
        + (f"; outliers {', '.join(sheet['outliers'])}" if sheet["outliers"] else "")
    )
    lines = [head]
    for o in sheet["outlier_cards"]:
        lines.append(f"sheet outlier {o['card_id']} [{', '.join(o['aspects'])}]: {o['note']}")
    return lines


def check_set(
    plan: SetPlan,
    finals: dict[str, Path],
    grammar: dict,
    sheet_review: dict | None = None,
    style: dict | None = None,
) -> SetEvaluation:
    notes: list[str] = []
    cards = plan.cards
    categories = Counter(str(c.category) for c in cards)
    colors = [c.background.color if c.background else None for c in cards]
    adjacent = [
        f"{a.card_id}/{b.card_id}"
        for a, b in zip(cards, cards[1:])
        if a.background and b.background and a.background.color == b.background.color
    ]
    hashes = {cid: dhash(p) for cid, p in finals.items() if p.exists()}
    dupes = [
        [a, b]
        for (a, ha), (b, hb) in combinations(sorted(hashes.items()), 2)
        if bin(ha ^ hb).count("1") <= DHASH_DUPLICATE_BITS
    ]
    fail = []
    # The same `object_count` rules as the plan validator: a set of 6–7 object cards (gold slots)
    # may miss one category; spreads scale with the object-card count.
    rules = object_count_rules(grammar, len(cards))
    missing_cats = {"1", "2", "3", "4"} - set(categories)
    if not set(categories) <= {"1", "2", "3", "4"} or (
        missing_cats and (rules["need_all_categories"] or len(missing_cats) > 1)
    ):
        fail.append(f"categories {dict(categories)}")
    elif missing_cats:
        notes.append(
            f"{len(cards)} object cards, category {sorted(missing_cats)} missing"
            " (allowed with gold slots)"
        )
    if len({c.scale_class for c in cards}) < rules["min_scales"]:
        fail.append("scale spread")
    if len({c.object_type for c in cards}) < rules["min_types"]:
        fail.append("type spread")
    if dupes:
        fail.append(f"near-duplicate images {dupes}")
    missing = [c.card_id for c in cards if c.card_id not in hashes]
    if missing:
        notes.append(f"{len(missing)} cards without an accepted image: {missing}")
    if adjacent:
        notes.append(f"adjacent equal background colours: {adjacent}")
    notes += _style_notes(style)
    notes += _sheet_notes(sheet_review)
    style_warn = bool(
        style and style.get("status") == "ok" and (style["outliers"] or not style["dispersion_ok"])
    )
    sheet_warn = bool(
        sheet_review
        and sheet_review.get("status") == "ok"
        and (sheet_review["outliers"] or not sheet_review["consistent_set"])
    )
    warn = missing or adjacent or style_warn or sheet_warn
    decision = "fail" if fail else "warn" if warn else "pass"
    return SetEvaluation(
        variant_id=plan.variant_id,
        category_distribution=dict(sorted(categories.items())),
        color_rhythm={
            "colors": colors,
            "distinct": len(set(colors)),
            "adjacent_repeats": adjacent,
        },
        scale_spread=sorted({c.scale_class for c in cards}),
        type_spread=sorted({c.object_type for c in cards}),
        duplicates=dupes,
        style_consistency=style,
        sheet_review=sheet_review,
        decision=decision,
        notes=fail + notes,
    )
