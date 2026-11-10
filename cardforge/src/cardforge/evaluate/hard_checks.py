"""Measured percentile thresholds; mask uncertainty remains visible."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageStat

from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.joint_checks import fit_overall
from cardforge.evaluate.joint_checks import score as joint_score
from cardforge.knowledge.catalog import load_cards

STAGE2_FEATURES = (
    "bbox_fill",
    "mask_area",
    "safe_overlap",
    "background_edges",
    "background_clusters",
)


def format_check(path: Path) -> dict:
    try:
        with Image.open(path) as image:
            image.load()
            valid = (
                image.format == "PNG" and image.size == (430, 480) and image.mode in ("RGB", "RGBA")
            )
            opaque = "A" not in image.getbands() or image.getchannel("A").getextrema() == (255, 255)
            nonconstant = max(ImageStat.Stat(image.convert("RGB")).stddev) > 0
            return {
                "status": "pass" if valid and opaque and nonconstant else "fail",
                "size": list(image.size),
                "mode": image.mode,
                "opaque": opaque,
                "nonconstant": nonconstant,
            }
    except OSError as exc:
        return {"status": "fail", "error": str(exc)}


def features(image_path: Path, mask_path: Path, zone: dict) -> dict:
    """Stage-2 geometry features; the definitions live in `mask_checks.measure`."""
    from cardforge.evaluate.mask_checks import measure

    with Image.open(image_path) as image, Image.open(mask_path) as mask:
        values = measure(image, mask, zone)
    return {k: values[k] for k in STAGE2_FEATURES}


def base_checks(row: dict, allowed_text: list[str] | None = None) -> dict:
    results = {"format": row["format"]}
    ocr = row["ocr"]
    allowed = {s.casefold().strip() for s in allowed_text or []}
    if ocr["status"] != "ok":
        results["text"] = {"status": "review", "reason": "OCR unavailable"}
    else:
        forbidden = [o for o in ocr["observations"] if o["text"].strip().casefold() not in allowed]
        results["text"] = {"status": "fail" if forbidden else "pass", "observations": forbidden}
    return results


def fit_all_checks(rows: list[dict]) -> dict:
    eligible = {
        row["item_id"]
        for row in rows
        if all(c["status"] == "pass" for c in base_checks(row).values())
    }
    return fit_overall(rows, eligible)


def evaluate_row(row: dict, checks: dict, allowed_text: list[str] | None = None) -> dict:
    results = base_checks(row, allowed_text)
    cat = str(row["category"])
    limits = checks["by_category"].get(cat, {})
    for name in ("bbox_fill", "safe_overlap", "background_edges", "background_clusters"):
        if name.startswith("background") and cat not in ("1", "2"):
            results[name] = {"status": "not_applicable"}
            continue
        value = row["features"].get(name)
        threshold = limits.get(name)
        if row["mask_quality"] != "usable" or value is None or threshold is None:
            results[name] = {
                "status": "review",
                "value": value,
                "reason": "untrusted mask, missing feature or threshold",
            }
        else:
            low, high = threshold
            passed = (low is None or value >= low) and value <= high
            results[name] = {
                "status": "pass" if passed else "fail",
                "value": value,
                "threshold": threshold,
            }
            if checks.get("joint_geometry"):
                results[name]["legacy_interval_outlier"] = not passed
                results[name]["status"] = "pass" if passed else "diagnostic_outlier"
    if checks.get("joint_geometry"):
        calibration = checks["joint_geometry"]
        value = (
            joint_score(row["features"], row["category"], calibration)
            if row["mask_quality"] == "usable"
            else None
        )
        results["geometry_joint"] = {
            "status": (
                "review"
                if value is None
                else "pass"
                if value <= calibration["threshold"]
                else "fail"
            ),
            "score": value,
            "threshold": calibration["threshold"],
            "reason": "Joint empirical geometry gate; untrusted masks never pass",
        }
    statuses = {v["status"] for v in results.values()}
    decision = "fail" if "fail" in statuses else "review" if "review" in statuses else "pass"
    return {"item_id": row["item_id"], "decision": decision, "checks": results}


def calibrate_checks(root: Path) -> dict:
    import numpy as np

    from cardforge.evaluate.ocr import recognize

    cards = [c for c in load_cards(root) if c.category <= 4]
    zone_path = root / "knowledge/frames/safe_zone.json"
    zone = json.loads(zone_path.read_text())
    paths = [(root / f"dataset/{c.card_ref}.png").resolve() for c in cards]
    ocr = recognize(paths, root)
    rows = []
    for card, image in zip(cards, paths, strict=True):
        if sha256(image) != card.source_sha256:
            raise ValueError(f"changed catalog source: {card.card_ref}")
        mask = root / card.measured.mask_path
        rows.append(
            {
                "item_id": card.card_ref,
                "image_sha256": sha256(image),
                "mask_sha256": sha256(mask),
                "category": card.category,
                "mask_quality": card.measured.mask_quality,
                "features": features(image, mask, zone),
                "format": format_check(image),
                "ocr": ocr[str(image)],
            }
        )
    limits = {}
    for cat in range(1, 5):
        trusted = [r for r in rows if r["category"] == cat and r["mask_quality"] == "usable"]
        limits[str(cat)] = {}
        for name in ("bbox_fill", "safe_overlap", "background_edges", "background_clusters"):
            if name.startswith("background") and cat > 2:
                continue
            values = [r["features"][name] for r in trusted if r["features"][name] is not None]
            if values:
                limits[str(cat)][name] = [
                    float(np.quantile(values, 0.05)) if name == "bbox_fill" else None,
                    float(np.quantile(values, 0.95)),
                ]
        limits[str(cat)]["sample_n"] = len(trusted)
    checks = {
        "version": "checks-v4-overall-coverage",
        "calibrated": True,
        "production_validated": False,
        "method": "joint ECDF geometry; >=95% overall hard-check coverage; "
        "legacy p05/p95 diagnostic only",
        "features": "mask>=128; bbox fraction; union of conservative bands; Canny100/200; "
        "eroded background; RGB bins width32 with >=1% background share",
        "safe_zone_sha256": sha256(zone_path),
        "by_category": limits,
        "joint_geometry": fit_all_checks(rows),
    }
    results = [evaluate_row(r, checks) for r in rows]
    counts = Counter(r["decision"] for r in results)
    report = {
        "n": len(rows),
        "counts": dict(counts),
        "pass_rate": counts["pass"] / len(rows),
        "acceptance_95pct": counts["pass"] / len(rows) >= 0.95,
        "note": "In-sample descriptive calibration, not held-out accuracy. Unknown != pass.",
        "results": results,
    }
    features_path = root / "dataset/analysis/evaluation/check-features.json"
    dump(features_path, rows)
    checks["source_measurements_sha256"] = sha256(features_path)
    review_path = root / "knowledge/catalog/mask_review.jsonl"
    if review_path.exists():
        checks["mask_review_sha256"] = sha256(review_path)
    dump(root / "knowledge/style_profile/checks.yaml", checks)
    dump(root / "docs/stages/evidence/02/checks-calibration.json", report)
    return report


def check_image(
    root: Path,
    image: Path,
    mask: Path,
    category: int,
    mask_quality: str = "unreviewed",
    allowed_text: list[str] | None = None,
) -> dict:
    from cardforge.evaluate.ocr import recognize

    if category not in (1, 2, 3, 4) or mask_quality not in ("usable", "unreviewed", "suspect"):
        raise ValueError("invalid category or mask quality")
    checks = json.loads((root / "knowledge/style_profile/checks.yaml").read_text())
    zone = root / "knowledge/frames/safe_zone.json"
    if checks["safe_zone_sha256"] != sha256(zone):
        raise ValueError("safe zone changed; recalibrate checks")
    image = image.resolve()
    row = {
        "item_id": image.stem,
        "category": category,
        "mask_quality": mask_quality,
        "format": format_check(image),
        "ocr": recognize([image], root)[str(image)],
        "features": features(image, mask, json.loads(zone.read_text())),
    }
    return evaluate_row(row, checks, allowed_text)
