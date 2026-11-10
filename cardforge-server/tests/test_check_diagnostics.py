from types import SimpleNamespace

from cardforge.evaluate.check_diagnostics import counterfactual, diagnose
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.hard_checks import evaluate_row


def test_optimistic_diagnostics_keep_actual_failures_and_original_unknowns():
    original = [
        {
            "decision": "review",
            "checks": {"format": {"status": "pass"}, "bbox_fill": {"status": "review"}},
        },
        {
            "decision": "fail",
            "checks": {"text": {"status": "fail"}, "bbox_fill": {"status": "fail"}},
        },
    ]
    result = counterfactual(original, resolve_masks=True, ignore_ocr=True)
    assert result["counts"] == {"pass": 1, "fail": 1}
    assert original[0]["checks"]["bbox_fill"]["status"] == "review"
    assert original[1]["checks"]["text"]["status"] == "fail"


def test_diagnostics_validate_the_reviewed_mask_path_not_an_old_raw_path(tmp_path, monkeypatch):
    image = tmp_path / "dataset/card.png"
    image.parent.mkdir()
    image.write_bytes(b"image fixture")
    mask = tmp_path / "dataset/corrected.png"
    mask.write_bytes(b"corrected fixture")
    row = {
        "item_id": "card",
        "category": 3,
        "mask_quality": "usable",
        "image_sha256": sha256(image),
        "mask_sha256": sha256(mask),
        "format": {"status": "pass"},
        "ocr": {"status": "ok", "observations": []},
        "features": {"bbox_fill": 0.5, "safe_overlap": 0.01},
    }
    checks = {"by_category": {"3": {"bbox_fill": [0.1, 0.9], "safe_overlap": [None, 0.1]}}}
    dump(tmp_path / "dataset/analysis/evaluation/check-features.json", [row])
    dump(tmp_path / "knowledge/style_profile/checks.yaml", checks)
    dump(
        tmp_path / "docs/stages/evidence/02/checks-calibration.json",
        {"pass_rate": 1.0, "results": [evaluate_row(row, checks)]},
    )
    monkeypatch.setattr(
        "cardforge.evaluate.check_diagnostics.load_cards",
        lambda _: [
            SimpleNamespace(
                card_ref="card",
                measured=SimpleNamespace(mask_path="dataset/corrected.png", mask_quality="usable"),
            )
        ],
    )
    report = diagnose(tmp_path, tmp_path / "diagnostic")
    assert report["baseline_counts"] == {"pass": 1}
