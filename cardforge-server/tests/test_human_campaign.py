import json
from datetime import UTC, datetime

import pytest
from PIL import Image

from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.human_campaign import validate_campaign


@pytest.fixture
def campaign(tmp_path):
    image = tmp_path / "runs/image.png"
    image.parent.mkdir()
    Image.new("RGB", (430, 480), "blue").save(image)
    item = {
        "item_id": "new-001",
        "image": "runs/image.png",
        "image_sha256": sha256(image),
        "cohort": "borderline",
        "spec": {"object_name": "fixture", "category": 2},
    }
    manifest = tmp_path / "runs/manifest.json"
    dump(manifest, {"items": [item]})
    session = tmp_path / "runs/review"
    dump(session / "session.json", {"items": [{**item, "review_id": "review-001"}]})
    dump(
        session / "ratings.json",
        {
            "review-001": {
                "review_id": "review-001",
                "image_sha256": item["image_sha256"],
                "item_id": item["item_id"],
                "reviewer": "human",
                "decision": "accept",
                "accepted": True,
                "source": "human",
                "note": "Accepted, with a suggestion to improve the background.",
                "reviewed_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    judge = {
        "provider": "host-agent",
        "host": "codex",
        "model": "fixture",
        "family": "OpenAI GPT",
        "role": "working",
    }
    answer = {
        "item_id": item["item_id"],
        "image_sha256": item["image_sha256"],
        "criteria": {
            k: {"passed": True, "note": "fixture"}
            for k in (
                "object_match",
                "category",
                "style",
                "thumbnail_readability",
                "composition",
                "artifacts",
                "theme",
            )
        },
        "style_uncertain": True,
        "issues": [],
        "fix_instruction": None,
    }
    frozen = tmp_path / "runs/frozen.json"
    dump(
        frozen,
        {
            "judge": judge,
            "records": [
                {
                    "item_id": item["item_id"],
                    "image_sha256": item["image_sha256"],
                    "cohort": "borderline",
                    "judge": judge,
                    "answer": answer,
                    "decision": "flag",
                    "pack_id": "fixture",
                }
            ],
        },
    )
    return tmp_path, manifest, session, frozen, sha256(frozen)


def test_accept_with_suggestions_is_preserved_and_flag_is_not_a_binary_pair(campaign):
    result = validate_campaign(*campaign)
    assert result["labels"][0]["accepted"] is True
    assert result["summary"]["binary_paired_n"] == 0
    assert result["summary"]["cohen_kappa"] is None


def test_changed_frozen_judge_and_incomplete_human_review_are_rejected(campaign):
    root, manifest, session, frozen, digest = campaign
    original = frozen.read_bytes()
    frozen.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="frozen"):
        validate_campaign(root, manifest, session, frozen, digest)
    frozen.write_bytes(original)
    dump(session / "ratings.json", {})
    with pytest.raises(ValueError, match="complete"):
        validate_campaign(root, manifest, session, frozen, digest)


def test_changed_review_spec_is_not_imported_as_the_original_question(campaign):
    root, manifest, session, frozen, digest = campaign
    data = json.loads((session / "session.json").read_text())
    data["items"][0]["spec"]["category"] = 4
    dump(session / "session.json", data)
    with pytest.raises(ValueError, match="provenance"):
        validate_campaign(root, manifest, session, frozen, digest)
