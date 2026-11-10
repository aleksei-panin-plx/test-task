import copy

from cardforge.evaluate.hard_checks import evaluate_row, fit_all_checks
from cardforge.evaluate.joint_checks import fit, score


def test_joint_gate_fails_extreme_geometry_and_never_passes_unknown_masks_or_ocr():
    rows = [
        {
            "category": 3,
            "mask_quality": "usable",
            "features": {"bbox_fill": 0.3 + i * 0.005, "safe_overlap": 0.01 + i * 0.001},
        }
        for i in range(40)
    ]
    calibration = fit(rows)
    assert calibration["calibration_geometry_passed"] >= 38
    assert (
        score({"bbox_fill": 0.999, "safe_overlap": 0.9}, 3, calibration) > calibration["threshold"]
    )
    checks = {
        "joint_geometry": calibration,
        "by_category": {"3": {"bbox_fill": [0.3, 0.5], "safe_overlap": [None, 0.05]}},
    }
    row = {
        **copy.deepcopy(rows[20]),
        "item_id": "fixture",
        "format": {"status": "pass"},
        "ocr": {"status": "ok", "observations": []},
    }
    assert evaluate_row(row, checks)["decision"] == "pass"
    row["mask_quality"] = "suspect"
    assert evaluate_row(row, checks)["decision"] == "review"
    row["mask_quality"] = "usable"
    row["ocr"] = {"status": "unavailable"}
    assert evaluate_row(row, checks)["decision"] == "review"
    row["ocr"] = {"status": "ok", "observations": [{"text": "unexpected title"}]}
    assert evaluate_row(row, checks)["decision"] == "fail"


def test_missing_category_and_nonfinite_features_remain_unresolved():
    calibration = fit(
        [
            {
                "category": 3,
                "mask_quality": "usable",
                "features": {"bbox_fill": 0.5, "safe_overlap": 0.02},
            }
        ]
    )
    assert score({"bbox_fill": 0.5, "safe_overlap": 0.02}, 1, calibration) is None
    assert score({"bbox_fill": float("nan"), "safe_overlap": 0.02}, 3, calibration) is None


def overall_rows():
    return [
        {
            "item_id": f"control-{i}",
            "category": 3,
            "mask_quality": "usable",
            "features": {"bbox_fill": 0.2 + i * 0.005, "safe_overlap": i * 0.001},
            "format": {"status": "pass"},
            "ocr": {"status": "ok", "observations": []},
        }
        for i in range(100)
    ]


def decisions(rows, calibration):
    checks = {
        "joint_geometry": calibration,
        "by_category": {"3": {"bbox_fill": [0, 1], "safe_overlap": [None, 1]}},
    }
    return [evaluate_row(row, checks) for row in rows]


def test_joint_total_coverage_preserves_ocr_failures_and_rejects_unseen_extremes():
    rows = overall_rows()
    for row in rows[20:25]:
        row["ocr"]["observations"] = [{"text": "unexpected caption"}]
    # Separate geometric p95 plus OCR can fail the requested total coverage.
    assert sum(r["decision"] == "pass" for r in decisions(rows, fit(rows))) < 95
    calibration = fit_all_checks(rows)
    actual = decisions(rows, calibration)
    assert calibration["overall_coverage_feasible"]
    assert calibration["overall_target_n"] == 95
    assert sum(r["decision"] == "pass" for r in actual) == 95
    assert all(row["checks"]["text"]["status"] == "fail" for row in actual[20:25])
    assert score({"bbox_fill": 1.0, "safe_overlap": 0.9}, 3, calibration) > calibration["threshold"]


def test_infeasible_mandatory_failure_rate_never_turns_into_a_relaxed_ocr_gate():
    rows = overall_rows()
    for row in rows[20:26]:
        row["ocr"]["observations"] = [{"text": "forbidden"}]
    calibration = fit_all_checks(rows)
    actual = decisions(rows, calibration)
    assert not calibration["overall_coverage_feasible"]
    assert sum(r["decision"] == "pass" for r in actual) <= 94
    assert all(row["checks"]["text"]["status"] == "fail" for row in actual[20:26])
    rows = overall_rows()
    rows[0]["mask_quality"] = "suspect"
    rows[1]["ocr"]["status"] = "unavailable"
    rows[2]["format"]["status"] = "fail"
    actual = decisions(rows, fit_all_checks(rows))
    assert actual[0]["decision"] == "review"
    assert actual[1]["checks"]["text"]["status"] == "review"
    assert actual[1]["decision"] != "pass"  # Another independent gate may fail.
    assert actual[2]["decision"] == "fail"
