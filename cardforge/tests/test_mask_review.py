from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from cardforge.evaluate.common import dump, sha256
from cardforge.knowledge.catalog import load_cards, read_jsonl, write_jsonl


@pytest.fixture()
def reviewed_root(tmp_path):
    project = Path(__file__).resolve().parents[1]
    row = read_jsonl(project / "knowledge/catalog/cards.jsonl")[3]
    (tmp_path / "dataset/analysis").mkdir(parents=True)
    source = tmp_path / f"dataset/{row['card_ref']}.png"
    Image.new("RGB", (430, 480), "yellow").save(source)
    original = tmp_path / "dataset/analysis/original.png"
    corrected = tmp_path / "dataset/analysis/corrected.png"
    Image.new("L", (430, 480), 0).save(original)
    mask = Image.new("L", (430, 480), 0)
    ImageDraw.Draw(mask).rectangle((100, 100, 299, 299), fill=255)
    mask.save(corrected)
    evidence = tmp_path / "review.json"
    dump(evidence, {"decision": "usable", "mask_sha256": sha256(corrected)})
    row["source_sha256"] = sha256(source)
    row["label_source"] = "human"
    row["measured"].update(mask_path="dataset/analysis/original.png", mask_quality="suspect")
    write_jsonl(tmp_path / "knowledge/catalog/cards.jsonl", [row])
    review = {
        "card_ref": row["card_ref"],
        "source_sha256": sha256(source),
        "original_mask_sha256": sha256(original),
        "mask_path": "dataset/analysis/corrected.png",
        "mask_sha256": sha256(corrected),
        "reviewer": "test-agent",
        "review_source": "agent-visual",
        "review_note": "Structural support recovered; coarse geometry only.",
        "method": "fixture",
        "decision": "usable",
        "evidence": "review.json",
        "evidence_sha256": sha256(evidence),
        "measurements": {
            **row["measured"],
            "fill_ratio": 40000 / (430 * 480),
            "object_bbox": [100, 100, 300, 300],
            "mask_path": "dataset/analysis/corrected.png",
            "mask_quality": "usable",
        },
    }
    write_jsonl(tmp_path / "knowledge/catalog/mask_review.jsonl", [review])
    return tmp_path


def test_mask_overlay_preserves_raw_measurements_and_human_semantics(reviewed_root):
    raw = load_cards(reviewed_root, apply_masks=False)[0]
    current = load_cards(reviewed_root)[0]
    assert raw.measured.mask_quality == "suspect"
    assert current.measured.mask_quality == "usable"
    assert current.measured.object_bbox == (100, 100, 300, 300)
    assert current.model_dump(exclude={"measured"}) == raw.model_dump(exclude={"measured"})
    assert current.label_source == "human"


@pytest.mark.parametrize(
    "target",
    [
        "dataset/CardC6_card_04.png",
        "dataset/analysis/original.png",
        "dataset/analysis/corrected.png",
        "review.json",
    ],
)
def test_mask_overlay_rejects_changed_source_mask_or_evidence(reviewed_root, target):
    path = reviewed_root / target
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        load_cards(reviewed_root)


def test_suspect_proposal_does_not_promote_a_mask(reviewed_root):
    path = reviewed_root / "knowledge/catalog/mask_review.jsonl"
    rows = read_jsonl(path)
    rows[0]["decision"] = "suspect"
    write_jsonl(path, rows)
    assert load_cards(reviewed_root)[0].measured.mask_quality == "suspect"


def test_reviewed_measurements_must_match_pixels(reviewed_root):
    path = reviewed_root / "knowledge/catalog/mask_review.jsonl"
    rows = read_jsonl(path)
    rows[0]["measurements"]["fill_ratio"] = 1.0
    write_jsonl(path, rows)
    with pytest.raises(ValueError, match="contradicts mask pixels"):
        load_cards(reviewed_root)


def test_duplicate_reviews_rejected(reviewed_root):
    path = reviewed_root / "knowledge/catalog/mask_review.jsonl"
    rows = read_jsonl(path)
    write_jsonl(path, rows * 2)
    with pytest.raises(ValueError, match="duplicate"):
        load_cards(reviewed_root)
    assert json.loads(path.read_text().splitlines()[0])["review_source"] == "agent-visual"
