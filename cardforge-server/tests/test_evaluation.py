from __future__ import annotations

import copy
import json
import subprocess
import sys
from hashlib import sha256 as sha256_digest
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from cardforge.evaluate.blind_test import chance_distribution, export_blind, score_blind
from cardforge.evaluate.calibration import calibrate, cohen_kappa, require_calibrated, roc_auc
from cardforge.evaluate.common import dump, private_path, sha256
from cardforge.evaluate.hard_checks import evaluate_row, features, format_check
from cardforge.evaluate.judge_pack import export_pack, import_labels, import_pack
from cardforge.evaluate.models import JudgeAnswer, Label
from cardforge.knowledge.catalog import write_jsonl


def test_imports_are_lightweight():
    code = """
import sys
import cardforge.cli
import cardforge.evaluate.hard_checks
import cardforge.evaluate.style_metric
import cardforge.evaluate.judge_pack
import cardforge.evaluate.local_batch
import cardforge.evaluate.csd_metric
assert not {'torch','transformers','mlx','cv2','numpy','rembg','onnxruntime'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_known_calibration_metrics_and_degenerate_kappa():
    assert roc_auc([0.9, 0.8], [0.2, 0.1]) == 1
    assert roc_auc([1, 0], [1, 0]) == 0.5
    assert roc_auc([], [1]) is None
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0
    assert cohen_kappa([True, False], [True, False]) == 1
    assert cohen_kappa([True], [True]) is None
    with pytest.raises(ValueError):
        roc_auc([float("nan")], [0])
    with pytest.raises(ValueError):
        require_calibrated({"runtime_allowed": False}, "host-agent:codex:test")


def test_blind_null_has_exact_chance_recall():
    distribution = chance_distribution(10)
    assert sum(distribution) == pytest.approx(1)
    assert sum(k * p for k, p in enumerate(distribution)) / 30 == pytest.approx(0.3)
    assert distribution[30] == pytest.approx((1 / 120) ** 10)


def test_format_rejects_monochrome_alpha_and_wrong_dimensions(tmp_path):
    path = tmp_path / "image.png"
    Image.new("RGB", (430, 480), "red").save(path)
    assert format_check(path)["status"] == "fail"
    image = Image.new("RGB", (430, 480), "red")
    ImageDraw.Draw(image).rectangle((100, 100, 200, 200), fill="blue")
    image.save(path)
    assert format_check(path)["status"] == "pass"
    transparent = image.convert("RGBA")
    transparent.putalpha(0)
    transparent.save(path)
    assert format_check(path)["status"] == "fail"
    image.resize((429, 480)).save(path)
    assert format_check(path)["status"] == "fail"


def test_bbox_is_not_mask_area_and_band_union_does_not_double_count(tmp_path):
    image, mask = tmp_path / "image.png", tmp_path / "mask.png"
    Image.new("RGB", (100, 100), "blue").save(image)
    m = Image.new("L", (100, 100), 0)
    draw = ImageDraw.Draw(m)
    draw.rectangle((0, 0, 9, 9), fill=255)
    draw.rectangle((80, 80, 89, 89), fill=255)
    m.save(mask)
    values = features(image, mask, {"top_pct": 0.1, "bottom_pct": 0.1, "side_pct": 0.1})
    assert values["mask_area"] == 0.02
    assert values["bbox_fill"] == 0.81
    assert values["safe_overlap"] == 0.5
    Image.new("L", (20, 20)).save(mask)
    with pytest.raises(ValueError, match="dimensions"):
        features(image, mask, {"top_pct": 0.1, "bottom_pct": 0.1, "side_pct": 0.1})


def test_untrusted_masks_and_missing_ocr_never_pass():
    row = {
        "item_id": "a",
        "category": 1,
        "mask_quality": "usable",
        "format": {"status": "pass"},
        "ocr": {"status": "ok", "observations": []},
        "features": {
            "bbox_fill": 0.5,
            "safe_overlap": 0.01,
            "background_edges": 0.02,
            "background_clusters": 3,
        },
    }
    checks = {
        "by_category": {
            "1": {
                "bbox_fill": [0.3, 0.7],
                "safe_overlap": [None, 0.02],
                "background_edges": [None, 0.04],
                "background_clusters": [None, 5],
            }
        }
    }
    assert evaluate_row(row, checks)["decision"] == "pass"
    row["mask_quality"] = "suspect"
    assert evaluate_row(row, checks)["decision"] == "review"
    row["mask_quality"] = "usable"
    row["ocr"] = {"status": "unavailable"}
    assert evaluate_row(row, checks)["decision"] == "review"
    row["ocr"] = {"status": "ok", "observations": [{"text": "HELLO WORLD"}]}
    assert evaluate_row(row, checks, ["hello"])["decision"] == "fail"
    assert evaluate_row(row, checks, ["hello world"])["decision"] == "pass"


def answer(row):
    return {
        "item_id": row["item_id"],
        "image_sha256": row["image_sha256"],
        "criteria": {
            name: {"passed": True, "note": "Fixture observation"}
            for name in [
                "object_match",
                "category",
                "style",
                "thumbnail_readability",
                "composition",
                "artifacts",
                "theme",
            ]
        },
        "style_uncertain": False,
        "issues": [],
        "fix_instruction": None,
    }


@pytest.fixture
def pack_fixture(tmp_path):
    source = tmp_path / "dataset/source.png"
    source.parent.mkdir()
    Image.new("RGB", (430, 480), "blue").save(source)
    refs = ["ref1", "ref2"]
    for name in refs:
        Image.new("RGB", (430, 480), "green").save(tmp_path / f"dataset/{name}.png")
    dump(tmp_path / "knowledge/style_profile/reference_cards_v1.yaml", {"style_references": refs})
    item = {
        "item_id": "secret",
        "image": "dataset/source.png",
        "image_sha256": sha256(source),
        "cohort": "real",
        "generator_family": "other",
        "spec": {"category": 1},
    }
    manifest = tmp_path / "calibration/manifest.json"
    dump(manifest, {"items": [item]})
    pack = tmp_path / "dataset/pack"
    result = export_pack(tmp_path, manifest, pack, working_family="test-family")
    public = json.loads((pack / "pack.json").read_text())
    response = {
        "pack_id": result["pack_id"],
        "judge": {
            "provider": "host-agent",
            "host": "codex",
            "model": "test",
            "family": "test-family",
            "role": "working",
        },
        "answers": [answer(public["items"][0])],
    }
    answers = tmp_path / "response.json"
    dump(answers, response)
    return tmp_path, pack, answers, response, public


def test_judge_cycle_hides_truth_and_records_model(pack_fixture):
    root, pack, answers, _, public = pack_fixture
    assert "secret" not in json.dumps(public) and "cohort" not in json.dumps(public)
    with Image.open(pack / public["items"][0]["thumbnail"]) as thumb:
        assert thumb.size == (215, 240)
    result = import_pack(root, pack, answers)
    assert result["imported"] == 1 and result["judge"]["model"] == "test"
    with pytest.raises(ValueError, match="already imported"):
        import_pack(root, pack, answers)


@pytest.mark.parametrize("identity_field, value", [
    ("provider", "gemini"), ("host", "claude-code"), ("model", "different-test-model"),
])
def test_one_pack_accepts_multiple_judges_without_overwriting_first(
    pack_fixture, identity_field, value
):
    root, pack, answers, response, _ = pack_fixture
    first = import_pack(root, pack, answers)
    first_path = Path(first["output"])
    original = first_path.read_bytes()
    digest = sha256_digest(b"host-agent:codex:test").hexdigest()
    assert first_path.name == f'{response["pack_id"]}-{digest}.jsonl'
    response["judge"][identity_field] = value
    dump(answers, response)
    second = import_pack(root, pack, answers)
    assert second["pack_id"] == first["pack_id"] and second["output"] != first["output"]
    assert first_path.read_bytes() == original
    assert json.loads(Path(second["output"]).read_text())["judge"][identity_field] == value
    assert len(list((root / "calibration/judgements").glob("*.jsonl"))) == 2
    with pytest.raises(ValueError, match="already imported for this judge"):
        import_pack(root, pack, answers)


@pytest.mark.parametrize("omit_pack_id", [False, True])
def test_legacy_import_blocks_same_judge_but_allows_another_and_stays_unchanged(
    pack_fixture, omit_pack_id
):
    root, pack, answers, response, _ = pack_fixture
    imported = import_pack(root, pack, answers)
    legacy = root / f'calibration/judgements/{response["pack_id"]}.jsonl'
    Path(imported["output"]).rename(legacy)
    if omit_pack_id:
        row = json.loads(legacy.read_text())
        row.pop("pack_id")
        write_jsonl(legacy, [row])
    original = legacy.read_bytes()
    with pytest.raises(ValueError, match="already imported for this judge"):
        import_pack(root, pack, answers)
    response["judge"]["host"] = "claude-code"
    dump(answers, response)
    second = import_pack(root, pack, answers)
    assert Path(second["output"]) != legacy
    assert legacy.read_bytes() == original
    assert len(list(legacy.parent.glob("*.jsonl"))) == 2


@pytest.mark.parametrize("independent", [False, True])
def test_duplicate_pair_cannot_be_reimported_by_renaming_or_changing_identity_metadata(
    pack_fixture, independent
):
    root, pack, answers, response, _ = pack_fixture
    imported = import_pack(root, pack, answers)
    renamed = root / "calibration/judgements/manually-named-frozen-evidence.jsonl"
    Path(imported["output"]).rename(renamed)
    original = renamed.read_bytes()
    response["judge"]["family"] = "changed-family"
    if independent:
        response["judge"]["role"] = "independent"
    response["judge"] = dict(reversed(response["judge"].items()))
    dump(answers, response)
    with pytest.raises(ValueError, match="already imported for this judge"):
        import_pack(root, pack, answers)
    assert renamed.read_bytes() == original
    assert list(renamed.parent.iterdir()) == [renamed]


def test_same_judge_can_import_a_different_pack(pack_fixture):
    root, pack, answers, response, _ = pack_fixture
    first = import_pack(root, pack, answers)
    other_pack = root / "dataset/another-pack"
    exported = export_pack(root, root / "calibration/manifest.json", other_pack)
    public = json.loads((other_pack / "pack.json").read_text())
    response["pack_id"] = exported["pack_id"]
    response["answers"] = [answer(public["items"][0])]
    dump(answers, response)
    second = import_pack(root, other_pack, answers)
    assert second["pack_id"] != first["pack_id"] and second["output"] != first["output"]
    assert len(list((root / "calibration/judgements").glob("*.jsonl"))) == 2


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "sha", "extra", "criteria", "packid"])
def test_judge_rejects_partial_forged_or_unstructured_answers(pack_fixture, mutation):
    root, pack, answers, response, _ = pack_fixture
    if mutation == "missing":
        response["answers"] = []
    elif mutation == "duplicate":
        response["answers"] *= 2
    elif mutation == "sha":
        response["answers"][0]["image_sha256"] = "0" * 64
    elif mutation == "extra":
        response["answers"][0]["overall_score"] = 10
    elif mutation == "packid":
        response["pack_id"] = "../../outside"
    else:
        response["answers"][0]["criteria"].pop("style")
    dump(answers, response)
    with pytest.raises(ValueError):
        import_pack(root, pack, answers)


def test_judge_rejects_changed_image_and_path_escape(pack_fixture):
    root, pack, answers, _, public = pack_fixture
    Image.new("RGB", (430, 480), "red").save(pack / public["items"][0]["image"])
    with pytest.raises(ValueError, match="changed judge input"):
        import_pack(root, pack, answers)
    with pytest.raises(ValueError, match="escapes"):
        private_path(root, "../secret")


@pytest.mark.parametrize("tamper_link", [False, True])
def test_generated_judge_assets_stay_in_runs_and_links_cannot_escape(pack_fixture, tamper_link):
    root, _, _, response, _ = pack_fixture
    manifest = root / "calibration/manifest.json"
    data = json.loads(manifest.read_text())
    data["items"][0]["cohort"] = "foreign"
    dump(manifest, data)
    with pytest.raises(ValueError, match="generated art"):
        export_pack(root, manifest, root / "dataset/forbidden")
    output = root / "runs/judge-pack"
    exported = export_pack(root, manifest, output)
    public = json.loads((output / "pack.json").read_text())
    source = output / public["items"][0]["image"]
    assert source.is_symlink() and source.resolve().is_relative_to(root / "runs")
    assert (output / "ref-1.png").resolve().is_relative_to(root / "dataset")
    response["pack_id"] = exported["pack_id"]
    response["answers"] = [answer(public["items"][0])]
    answers = output / "answers.json"
    dump(answers, response)
    if tamper_link:
        source.unlink()
        (root / ".env").write_text("fixture, never read as a judge image")
        source.symlink_to(root / ".env")
        with pytest.raises(ValueError, match="private art folders"):
            import_pack(root, output, answers)
    else:
        assert import_pack(root, output, answers)["imported"] == 1


@pytest.mark.parametrize("source", ["reference", "agent"])
def test_no_fabricated_human_borderline_labels(source):
    with pytest.raises(ValueError, match="human-labelled"):
        Label(
            item_id="a",
            image_sha256="0" * 64,
            cohort="borderline",
            accepted=True,
            source=source,
            reviewer="agent",
            note="",
        )


def test_human_import_rejects_stale_image(pack_fixture):
    root, _, _, _, _ = pack_fixture
    source = root / "human.jsonl"
    write_jsonl(root / "calibration/labels.jsonl", [])
    write_jsonl(
        source,
        [
            {
                "item_id": "secret",
                "image_sha256": "0" * 64,
                "cohort": "real",
                "accepted": True,
                "source": "human",
                "reviewer": "human",
                "note": "",
            }
        ],
    )
    with pytest.raises(ValueError, match="stale image"):
        import_labels(root, source)


def test_style_self_exclusion_normalization_and_finite_inputs():
    np = pytest.importorskip("numpy")
    from cardforge.evaluate.style_metric import fit, score

    vectors = np.eye(8)
    ids, hashes = [str(i) for i in range(8)], [str(i) * 64 for i in range(8)]
    index = fit(vectors, ids, hashes)
    result = score(vectors[0] * 2, index, image_sha256=hashes[0])
    assert result["centroid_distance"] == pytest.approx(1)
    assert all(r["item_id"] != "0" for r in result["nearest"])
    assert result["knn_distance"] == pytest.approx(1)
    with pytest.raises(ValueError, match="duplicate"):
        fit(vectors, ["a"] * 8, hashes)
    bad = copy.deepcopy(vectors)
    bad[0, 0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        fit(bad, ids, hashes)


def test_uncertain_style_is_flag_and_failure_requires_repair():
    row = answer({"item_id": "a", "image_sha256": "0" * 64})
    row["style_uncertain"] = True
    assert JudgeAnswer.model_validate(row).decision == "flag"
    row["criteria"]["category"]["passed"] = False
    with pytest.raises(ValueError, match="fix_instruction"):
        JudgeAnswer.model_validate(row)


@pytest.mark.parametrize("same_family", [True, False])
def test_independence_is_against_working_judge_family(pack_fixture, same_family):
    root, pack, answers, response, _ = pack_fixture
    response["judge"]["role"] = "independent"
    response["judge"]["family"] = "test-family" if same_family else "different-family"
    dump(answers, response)
    if same_family:
        with pytest.raises(ValueError, match="different working judge"):
            import_pack(root, pack, answers)
    else:
        assert import_pack(root, pack, answers)["imported"] == 1


def test_blind_cycle_requires_all_sheets_and_unchanged_inputs(tmp_path):
    folder = tmp_path / "dataset"
    folder.mkdir()
    items = []
    for i in range(10):
        source = folder / f"{i}.png"
        Image.new("RGB", (430, 480), (i * 20, 20, 180)).save(source)
        items.append(
            {
                "item_id": str(i),
                "image": f"dataset/{i}.png",
                "image_sha256": sha256(source),
                "cohort": "real",
                "spec": {"set_theme": str(i), "category": 1},
            }
        )
    manifest = tmp_path / "manifest.json"
    dump(manifest, {"items": items})
    pack = folder / "blind"
    result = export_blind(tmp_path, manifest, pack, seed=42, working_family="A")
    response = pack / "response.json"
    data = {
        "pack_id": result["pack_id"],
        "reviewer": "fixture",
        "family": "B",
        "answers": [{"sheet": f"sheet-{i:02}.png", "positions": [1, 2, 3]} for i in range(1, 11)],
    }
    dump(response, data)
    assert score_blind(tmp_path, pack, response)["independent_family_verified"]
    data["answers"].pop()
    dump(response, data)
    with pytest.raises(ValueError, match="all sheets"):
        score_blind(tmp_path, pack, response)
    Image.new("RGB", (1, 1)).save(pack / "sheet-01.png")
    with pytest.raises(ValueError, match="changed blind-test"):
        score_blind(tmp_path, pack, response)


def test_runtime_calibration_needs_full_coverage_and_unmodified_decisions(tmp_path, monkeypatch):
    labels, records, items = [], [], []
    for i in range(230):
        cohort = "real" if i < 160 else "foreign" if i < 190 else "borderline"
        truth = cohort == "real" or (cohort == "borderline" and i % 2 == 0)
        label = {
            "item_id": str(i),
            "image_sha256": "0" * 64,
            "cohort": cohort,
            "accepted": truth,
            "source": "reference" if cohort == "real" else "human",
            "reviewer": "fixture",
            "note": "Fixture, not acceptance evidence",
        }
        labels.append(label)
        items.append(label)
        evaluation = answer(label)
        if not truth:
            for name in ("object_match", "style", "category"):
                evaluation["criteria"][name]["passed"] = False
            evaluation["fix_instruction"] = "Repair fixture"
        records.append(
            {
                "item_id": str(i),
                "image_sha256": "0" * 64,
                "cohort": cohort,
                "judge": {
                    "provider": "host-agent",
                    "host": "codex",
                    "model": "test",
                    "family": "test",
                    "role": "working",
                },
                "answer": evaluation,
                "decision": "accept" if truth else "reject",
            }
        )
    monkeypatch.setattr("cardforge.evaluate.calibration.load_manifest", lambda *args: items)
    monkeypatch.setattr(
        "cardforge.evaluate.calibration.load_cards",
        lambda *args: [SimpleNamespace(card_ref=str(i)) for i in range(160)],
    )
    path = tmp_path / "calibration/judgements/test.jsonl"
    write_jsonl(tmp_path / "calibration/labels.jsonl", labels)
    write_jsonl(path, records)
    report = calibrate(tmp_path, "host-agent:codex:test")
    assert report["runtime_allowed"] and report["cohen_kappa"] == 1
    independent = copy.deepcopy(records[0])
    independent["judge"]["role"] = "independent"
    write_jsonl(path, records + [independent])
    assert calibrate(tmp_path, "host-agent:codex:test")["runtime_allowed"]
    diagnostic = calibrate(tmp_path, "host-agent:codex:test", role="independent")
    assert diagnostic["real_n"] == 1 and not diagnostic["runtime_allowed"]
    records[-1]["rubric_version"] = "rubric7-human-guidance-v2"
    write_jsonl(path, records)
    with pytest.raises(ValueError, match="rubric versions"):
        calibrate(tmp_path, "host-agent:codex:test")
    records[-1].pop("rubric_version")
    records[-1]["input_contract_version"] = "judge-inputs-v3"
    write_jsonl(path, records)
    with pytest.raises(ValueError, match="different judge inputs"):
        calibrate(tmp_path, "host-agent:codex:test")
    records[-1].pop("input_contract_version")
    records[-1]["judge_run_settings"] = {"thinking_level": "MEDIUM"}
    write_jsonl(path, records)
    with pytest.raises(ValueError, match="different inference settings"):
        calibrate(tmp_path, "host-agent:codex:test")
    records[-1].pop("judge_run_settings")
    records[-1]["answer"]["style_uncertain"] = True
    records[-1]["decision"] = "flag"
    write_jsonl(path, records)
    assert not calibrate(tmp_path, "host-agent:codex:test")["runtime_allowed"]
    records[-1]["decision"] = "accept"
    write_jsonl(path, records)
    with pytest.raises(ValueError, match="contradicts"):
        calibrate(tmp_path, "host-agent:codex:test")


def test_style_auc_uses_distinct_full_controls_and_does_not_pass_small_sample():
    np = pytest.importorskip("numpy")
    from cardforge.evaluate.style_metric import compare_embeddings, fit

    vectors = np.asarray([[1.0, v] for v in np.linspace(-0.1, 0.1, 8)])
    ids, hashes = [str(i) for i in range(8)], [str(i) * 64 for i in range(8)]
    index = fit(vectors, ids, hashes)
    real = {identifier: {"image_sha256": h} for identifier, h in zip(ids, hashes, strict=True)}
    foreign = [
        {"item_id": f"f{i}", "matched_reference_id": str(i % 2), "foreign_style": style}
        for i, style in enumerate(["flat_2d", "photorealism", "other_3d"] * 2)
    ]
    report = compare_embeddings(index, foreign, [[0.0, 1.0]] * 6, real)
    assert report["auc"] == 1 and report["real_n"] == 8
    assert report["foreign_n"] == 6 and not report["style_content_separation_validated"]
    real["0"]["image_sha256"] = "stale"
    with pytest.raises(ValueError, match="stale reference"):
        compare_embeddings(index, foreign, [[0.0, 1.0]] * 6, real)


def test_definite_failure_policy_never_expands_acceptance_and_preserves_raw_answers():
    names = ['object_match', 'category', 'style', 'thumbnail_readability',
             'composition', 'artifacts', 'theme']
    for mask in range(128):
        for uncertain in (False, True):
            raw = {
                'item_id': 'synthetic', 'image_sha256': '0' * 64,
                'criteria': {name: {'passed': bool(mask & (1 << i)), 'note': 'Fixture'}
                             for i, name in enumerate(names)},
                'style_uncertain': uncertain, 'issues': [], 'fix_instruction': 'Fix fixture',
            }
            answer = JudgeAnswer.model_validate(raw)
            before = answer.model_dump()
            old = answer.decision
            new = answer.decision_for('definite-failure-first-v1')
            assert (old == 'accept') == (new == 'accept')
            assert answer.model_dump() == before
            other_failure = any(not raw['criteria'][n]['passed'] for n in names if n != 'style')
            if uncertain and not other_failure:
                assert new == 'flag'
            if uncertain and other_failure:
                assert old == 'flag' and new in ('fix', 'reject')
                assert answer.style_uncertain is True
