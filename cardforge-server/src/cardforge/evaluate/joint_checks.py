"""Joint geometry calibration. No ML imports; unknown measurements remain unknown."""

from __future__ import annotations

import math

FEATURES = ("bbox_fill", "safe_overlap", "background_edges", "background_clusters")


def score(features: dict, category: int, calibration: dict) -> float | None:
    peers = calibration["reference_by_category"].get(str(category))
    if not peers:
        return None
    parts = []
    for name in FEATURES:
        if name.startswith("background") and category > 2:
            continue
        value, values = features.get(name), peers.get(name, [])
        if value is None or not math.isfinite(value) or not values:
            return None
        percentile = (sum(v < value for v in values) + 0.5 * sum(v == value for v in values)) / len(
            values
        )
        parts.append(abs(2 * percentile - 1) if name == "bbox_fill" else percentile)
    return max(parts)


def fit(rows: list[dict]) -> dict:
    import numpy as np

    trusted = [r for r in rows if r["mask_quality"] == "usable"]
    if not trusted:
        raise ValueError("usable calibration masks required")
    references = {}
    for category in sorted({r["category"] for r in trusted}):
        peers = [r for r in trusted if r["category"] == category]
        references[str(category)] = {
            name: sorted(r["features"][name] for r in peers)
            for name in FEATURES
            if not name.startswith("background") or category <= 2
        }
    calibration = {
        "target_coverage": 0.95,
        "reference_by_category": references,
        "sample_n": len(trusted),
        "method": "midrank ECDF maximum; p95 higher",
        "validation_scope": "in-sample descriptive calibration, not held-out accuracy",
    }
    scores = [score(r["features"], r["category"], calibration) for r in trusted]
    if any(s is None or not math.isfinite(s) for s in scores):
        raise ValueError("all usable masks require finite applicable measurements")
    calibration["threshold"] = float(np.quantile(scores, 0.95, method="higher"))
    calibration["calibration_geometry_passed"] = sum(s <= calibration["threshold"] for s in scores)
    return calibration


def fit_overall(rows: list[dict], eligible_ids: set[str]) -> dict:
    """Calibrate the geometric threshold for overall hard-check coverage.

    Non-geometric mandatory failures are never overridden. They consume the same
    rejection budget as geometry, rather than adding another rejection rate after
    a separate geometric p95. Unknown masks cannot enter the passing pool.
    """
    identifiers = [r["item_id"] for r in rows]
    if len(set(identifiers)) != len(identifiers) or eligible_ids - set(identifiers):
        raise ValueError("unique reference IDs and known eligibility required")
    calibration = fit(rows)
    required = math.ceil(calibration["target_coverage"] * len(rows))
    values = [
        score(r["features"], r["category"], calibration)
        for r in rows
        if r["mask_quality"] == "usable" and r["item_id"] in eligible_ids
    ]
    feasible = len(values) >= required
    if feasible:
        # The smallest empirical boundary meeting total coverage, including ties.
        calibration["threshold"] = sorted(values)[required - 1]
    calibration.update(
        coverage_scope="all mandatory checks",
        overall_reference_n=len(rows),
        overall_target_n=required,
        non_geometry_eligible_n=len(eligible_ids),
        eligible_trusted_n=len(values),
        overall_coverage_feasible=feasible,
        eligible_geometry_passed=sum(v <= calibration["threshold"] for v in values),
        method="midrank ECDF geometry maximum; minimal finite threshold for >=95% "
        "overall mandatory-check coverage; independent hard failures preserved",
    )
    calibration["calibration_geometry_passed"] = sum(
        score(r["features"], r["category"], calibration) <= calibration["threshold"]
        for r in rows
        if r["mask_quality"] == "usable"
    )
    return calibration
