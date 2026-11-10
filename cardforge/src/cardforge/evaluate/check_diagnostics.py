"""Read-only diagnostics and a joint-gate experiment, never a production threshold update."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.hard_checks import evaluate_row
from cardforge.knowledge.catalog import load_cards


def counts(results: list[dict]) -> dict:
    return dict(Counter(r["decision"] for r in results))


def counterfactual(results: list[dict], *, resolve_masks=False, ignore_ocr=False) -> dict:
    """Optimistic accounting only; never exported as actual passing decisions."""
    outcomes = []
    for row in results:
        states = []
        for name, check in row["checks"].items():
            status = check["status"]
            if resolve_masks and name not in ("format", "text") and status == "review":
                status = "pass"
            if ignore_ocr and name == "text":
                status = "pass"
            states.append(status)
        outcomes.append(
            {"decision": "fail" if "fail" in states else "review" if "review" in states else "pass"}
        )
    return {
        "counts": counts(outcomes),
        "pass_rate": sum(r["decision"] == "pass" for r in outcomes) / len(outcomes),
        "scope": "Counterfactual: unknown masks and/or OCR forced to pass, not evidence.",
    }


def joint_experiment(rows: list[dict]) -> dict:
    """Joint empirical 95% gate over trusted masks, exploratory and in-sample only."""
    import numpy as np

    trusted = [r for r in rows if r["mask_quality"] == "usable"]
    scores = []
    for row in trusted:
        parts = []
        peers = [p for p in trusted if p["category"] == row["category"]]
        for name in ("bbox_fill", "safe_overlap", "background_edges", "background_clusters"):
            if name.startswith("background") and row["category"] > 2:
                continue
            value = row["features"][name]
            values = [p["features"][name] for p in peers]
            rank = sum(v < value for v in values) + 0.5 * sum(v == value for v in values)
            percentile = rank / len(values)
            parts.append(abs(2 * percentile - 1) if name == "bbox_fill" else percentile)
        scores.append({"item_id": row["item_id"], "score": max(parts)})
    threshold = float(np.quantile([r["score"] for r in scores], 0.95, method="higher"))
    passed = sum(r["score"] <= threshold for r in scores)
    return {
        "production_enabled": False,
        "threshold": threshold,
        "quantile_method": "higher: include the boundary observation in a finite sample",
        "trusted_masks": len(scores),
        "geometry_passed": passed,
        "geometry_pass_rate": passed / len(scores),
        "untrusted_masks": len(rows) - len(scores),
        "scores": scores,
        "scope": "Exploratory in-sample joint ECDF maximum. Does not resolve unknown masks "
        "or OCR, is not held-out validation and does not change p05/p95 rules. "
        "Changing the architecture calibration rule requires an explicit decision.",
    }


def diagnose(root: Path, output: Path) -> dict:
    if (output / "report.json").exists():
        raise ValueError("diagnostic snapshot exists; use a new variant directory")
    rows_path = root / "dataset/analysis/evaluation/check-features.json"
    limits_path = root / "knowledge/style_profile/checks.yaml"
    rows = json.loads(rows_path.read_text())
    checks = json.loads(limits_path.read_text())
    cards = {c.card_ref: c for c in load_cards(root)}
    for row in rows:
        image = root / f"dataset/{row['item_id']}.png"
        card = cards[row["item_id"]]
        mask = root / card.measured.mask_path
        if (
            sha256(image) != row["image_sha256"]
            or sha256(mask) != row["mask_sha256"]
            or card.measured.mask_quality != row["mask_quality"]
        ):
            raise ValueError("diagnostic inputs changed since actual measurement")
    results = [evaluate_row(r, checks) for r in rows]
    original = json.loads((root / "docs/stages/evidence/02/checks-calibration.json").read_text())
    if results != original["results"]:
        raise ValueError("baseline decisions are not reproducible")
    failures = Counter(
        name for r in results for name, c in r["checks"].items() if c["status"] == "fail"
    )
    reviews = Counter(
        name for r in results for name, c in r["checks"].items() if c["status"] == "review"
    )
    report = {
        "measured_n": len(rows),
        "baseline_counts": counts(results),
        "baseline_pass_rate": original["pass_rate"],
        "input_sha256": {str(p.relative_to(root)): sha256(p) for p in (rows_path, limits_path)},
        "failure_counts_overlapping": dict(failures),
        "review_counts_overlapping": dict(reviews),
        "mask_quality_by_category": {
            str(cat): dict(Counter(r["mask_quality"] for r in rows if r["category"] == cat))
            for cat in (1, 2, 3, 4)
        },
        "optimistic_mask_resolution_only": counterfactual(results, resolve_masks=True),
        "optimistic_ocr_removal_only": counterfactual(results, ignore_ocr=True),
        "optimistic_both": counterfactual(results, resolve_masks=True, ignore_ocr=True),
        "joint_gate_experiment": joint_experiment(rows),
        "quantile_conflict": "p05/p95 retains about 90% for a continuous scalar before "
        "intersecting checks; 95% overall does not follow from that rule.",
        "thresholds_changed": False,
        "mask_quality_promoted": False,
        "ocr_observations_changed": False,
        "stage_criterion_met": False,
    }
    dump(output / "report.json", report)
    return report
