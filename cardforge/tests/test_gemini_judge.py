"""Offline SDK-contract and no-repeat checks; all provider methods are replaced."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evaluation import pack_fixture as pack_fixture

from cardforge.evaluate.api_budget import Ledger
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.gemini_judge import run
from cardforge.evaluate.judge_pack import Submission


@pytest.fixture
def fake_api(pack_fixture, monkeypatch):
    genai = pytest.importorskip("google.genai")
    root, pack, _answers, response, _public = pack_fixture
    project = Path(__file__).resolve().parents[1]
    (root / "config").mkdir()
    (root / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=5.0\npaid_api_enabled=true\n"
    )
    (root / "config/evaluation.toml").write_text((project / "config/evaluation.toml").read_text())
    (root / ".env").write_text("GEMINI_API_KEY=offline-fixture-not-a-real-key\n")
    state = {"generate": 0, "count": 0, "error": None}

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["vertexai"] is False
            assert kwargs["http_options"].retry_options.attempts == 1
            state["http_timeout"] = kwargs["http_options"].timeout
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def count_tokens(self, *, model, contents):
            state["count"] += 1
            return SimpleNamespace(total_tokens=1000)

        def generate_content(self, *, model, contents, config):
            state["generate"] += 1
            state["service_tier"] = getattr(config.service_tier, "value", config.service_tier)
            state["part_texts"] = [part.text for part in contents if part.text]
            state["thinking_level"] = getattr(
                config.thinking_config.thinking_level,
                "value",
                config.thinking_config.thinking_level,
            )
            assert config.response_modalities == ["TEXT"]
            assert config.automatic_continuation is False
            assert config.automatic_function_calling.disable is True
            assert config.max_output_tokens == 4096
            if state["error"]:
                raise state["error"]
            return SimpleNamespace(
                sdk_http_response=SimpleNamespace(headers=state.get("response_headers", {})),
                text=json.dumps(response["answers"][0]),
                model_version=model,
                candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(
                    model_dump=lambda **_kwargs: {
                        "prompt_token_count": 1000,
                        "candidates_token_count": 500,
                        "thoughts_token_count": 100,
                        "total_token_count": 1600,
                    }
                ),
            )

    monkeypatch.setattr(genai, "Client", FakeClient)
    return root, pack, state


def test_saved_answers_resume_without_another_count_or_generate_request(fake_api):
    root, pack, state = fake_api
    first = run(root, pack)
    second = run(root, pack)
    assert first["complete"] and second["new_answers"] == 0
    assert state["generate"] == state["count"] == 1
    assert first["budget"] == second["budget"]
    assert first["budget"]["accounted_upper_usd"] == "0.003"
    Submission.model_validate_json((pack / "answers.gemini.json").read_text())


def test_flex_uses_requested_tier_long_timeout_and_published_rates(fake_api):
    root, pack, state = fake_api
    state["response_headers"] = {"X-Gemini-Service-Tier": "flex"}
    result = run(root, pack, service_tier="flex")
    assert state["service_tier"] == "flex"
    assert state["http_timeout"] == 900000
    assert result["budget"]["accounted_upper_usd"] == "0.0015"
    assert result["budget"]["unsettled"] == 0
    assert run(root, pack, service_tier="flex")["new_answers"] == 0
    assert state["generate"] == 1


def test_flex_reserves_at_standard_ceiling_before_sending(fake_api):
    root, pack, state = fake_api
    (root / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=0.02\npaid_api_enabled=true\n"
    )
    with pytest.raises(ValueError):
        run(root, pack, service_tier="flex")
    assert state["generate"] == 0


def test_service_tier_change_cannot_reuse_standard_response(fake_api):
    root, pack, state = fake_api
    run(root, pack)
    with pytest.raises(ValueError, match="different frozen request"):
        run(root, pack, service_tier="flex")
    assert state["generate"] == 1


def test_frozen_flex_pack_blocks_standard_before_any_provider_request(fake_api):
    root, pack, state = fake_api
    public = json.loads((pack / "pack.json").read_text())
    public["run_settings"] = {"service_tier": "flex"}
    dump(pack / "pack.json", public)
    key_path = root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
    key = json.loads(key_path.read_text())
    key["files"]["pack.json"] = sha256(pack / "pack.json")
    dump(key_path, key)
    with pytest.raises(ValueError, match="service tier contradicts"):
        run(root, pack)
    assert state["generate"] == state["count"] == 0


def test_explicit_standard_response_is_accounted_without_fallback_or_retry(fake_api):
    root, pack, state = fake_api
    state["response_headers"] = {"x-gemini-service-tier": "standard"}
    with pytest.raises(ValueError, match="unexpected billed tier"):
        run(root, pack, service_tier="flex")
    summary = Ledger(root / "runs/_api/ledger.json", 5).summary()
    assert summary["accounted_upper_usd"] == "0.003"
    assert summary["unsettled"] == 0
    assert state["generate"] == 1


def test_unknown_billing_tier_preserves_reservation_and_stops(fake_api):
    root, pack, state = fake_api
    state["response_headers"] = {"x-gemini-service-tier": "unexpected"}
    with pytest.raises(ValueError, match="reservation retained"):
        run(root, pack, service_tier="flex")
    assert Ledger(root / "runs/_api/ledger.json", 5).summary()["unsettled"] == 1
    assert state["generate"] == 1


def test_ambiguous_network_failure_keeps_reservation_and_is_not_blindly_retried(fake_api):
    root, pack, state = fake_api
    state["error"] = TimeoutError("offline simulated network failure")
    with pytest.raises(TimeoutError):
        run(root, pack)
    assert Ledger(root / "runs/_api/ledger.json", 5).summary()["unsettled"] == 1
    with pytest.raises(ValueError, match="already accounted"):
        run(root, pack)
    assert state["generate"] == 1


def test_no_positive_budget_means_no_provider_requests(fake_api):
    root, pack, state = fake_api
    (root / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=0\npaid_api_enabled=true\n"
    )
    with pytest.raises(ValueError, match="authorized budget"):
        run(root, pack)
    assert state["generate"] == state["count"] == 0


def test_different_thinking_settings_cannot_overwrite_a_frozen_response(fake_api):
    root, pack, state = fake_api
    run(root, pack, thinking_level="MEDIUM")
    assert state["thinking_level"] == "MEDIUM"
    with pytest.raises(ValueError, match="different frozen request"):
        run(root, pack, thinking_level="LOW")
    assert state["generate"] == state["count"] == 1


def test_three_reference_roles_are_sent_with_their_labels(fake_api):
    from PIL import Image

    root, pack, state = fake_api
    public = json.loads((pack / "pack.json").read_text())
    Image.new("RGB", (430, 480), "purple").save(pack / "ref-3.png")
    item = public["items"][0]
    item["refs"].append("ref-3.png")
    item["reference_context"] = [
        {"image": name, "role": role, "category": 1}
        for name, role in zip(item["refs"], ("style", "category", "material"), strict=True)
    ]
    public["rubric_version"] = "rubric7-human-guidance-v3"
    dump(pack / "pack.json", public)
    registry = root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
    key = json.loads(registry.read_text())
    key["files"].update(
        {"pack.json": sha256(pack / "pack.json"), "ref-3.png": sha256(pack / "ref-3.png")}
    )
    dump(registry, key)
    with pytest.raises(ValueError, match="unexpected evaluation rubric"):
        run(root, pack)
    assert state["generate"] == state["count"] == 0
    result = run(root, pack, expected_rubric="rubric7-human-guidance-v3")
    assert result["complete"]
    assert all(
        f"Reference: {role}, category 1" in state["part_texts"]
        for role in ("style", "category", "material")
    )


def test_teaching_is_explicit_and_group_overlap_stops_before_any_request(fake_api):
    from PIL import Image

    root, pack, state = fake_api
    source = root / "runs/teacher.png"
    source.parent.mkdir(exist_ok=True)
    Image.new("RGB", (215, 240), "cyan").save(source)
    (pack / "teaching-001.png").write_bytes(source.read_bytes())
    public = json.loads((pack / "pack.json").read_text())
    public["rubric_version"] = "rubric7-human-guidance-v4-fewshot"
    public["teacher_examples"] = [
        {
            "example_id": "example-001",
            "image": "teaching-001.png",
            "accepted": True,
            "note": "Synthetic fixture; not a live human label.",
            "spec": {"object_name": "fixture", "category": 1, "set_theme": "fixture"},
        }
    ]
    dump(pack / "pack.json", public)
    registry = root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
    key = json.loads(registry.read_text())
    for item in key["mapping"].values():
        item["reference_group"] = "held-out-object"
    key["teacher_examples"] = [
        {
            "item": {
                "item_id": "teaching-fixture",
                "image": "runs/teacher.png",
                "image_sha256": sha256(source),
                "reference_group": "teaching-object",
            },
            "label": {
                "item_id": "teaching-fixture",
                "image_sha256": sha256(source),
                "cohort": "borderline",
                "accepted": True,
                "source": "human",
                "reviewer": "fixture",
                "note": "Synthetic fixture only.",
            },
        }
    ]
    key["files"].update(
        {
            "pack.json": sha256(pack / "pack.json"),
            "teaching-001.png": sha256(pack / "teaching-001.png"),
        }
    )
    dump(registry, key)
    with pytest.raises(ValueError, match="explicit teaching-data approval"):
        run(root, pack, expected_rubric="rubric7-human-guidance-v4-fewshot")
    assert state["generate"] == state["count"] == 0
    preview = pack.parent / "approval-preview.json"
    dump(preview, public["teacher_examples"])
    dump(
        pack.parent / "approval-status.json",
        {
            "state": "approved",
            "pack_id": public["pack_id"],
            "preview_sha256": sha256(preview),
            "teaching_image_sha256": {"teaching-001.png": sha256(pack / "teaching-001.png")},
        },
    )
    result = run(root, pack, expected_rubric="rubric7-human-guidance-v4-fewshot")
    assert result["complete"]
    assert any("Обучающий пример" in text for text in state["part_texts"])
    original_note = public["teacher_examples"][0]["note"]
    public["teacher_examples"][0]["note"] = "A different, unapproved annotation"
    dump(pack / "pack.json", public)
    key["files"]["pack.json"] = sha256(pack / "pack.json")
    dump(registry, key)
    with pytest.raises(ValueError, match="does not cover the exact"):
        run(root, pack, expected_rubric="rubric7-human-guidance-v4-fewshot")
    public["teacher_examples"][0]["note"] = original_note
    dump(pack / "pack.json", public)
    key["files"]["pack.json"] = sha256(pack / "pack.json")
    for item in key["mapping"].values():
        item["reference_group"] = "teaching-object"
    dump(registry, key)
    with pytest.raises(ValueError, match="object group overlaps"):
        run(root, pack, expected_rubric="rubric7-human-guidance-v4-fewshot")
    assert state["generate"] == state["count"] == 1


def test_received_flex_503_is_audited_as_nonbillable_and_not_automatically_retried(fake_api):
    from google.genai.errors import ServerError

    from cardforge.evaluate.gemini_judge import FlexCapacityUnavailable

    root, pack, state = fake_api
    state['error'] = ServerError(503, {'error': {'code': 503, 'status': 'UNAVAILABLE',
                                               'message': 'Synthetic provider capacity failure'}})
    with pytest.raises(FlexCapacityUnavailable):
        run(root, pack, service_tier='flex')
    summary = Ledger(root / 'runs/_api/ledger.json', 5).summary()
    assert state['generate'] == 1 and summary['unsettled'] == 0
    assert summary['accounted_upper_usd'] == '0'
    receipts = list((pack / 'gemini-responses').glob('*.error.json'))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())['known_terminal_provider_error']
    state['error'] = None
    result = run(root, pack, service_tier='flex', attempt_tag='explicit-after-503')
    assert result['complete'] and state['generate'] == 2
    assert result['budget']['requests'] == 2
    assert result['budget']['accounted_upper_usd'] == '0.0015'


def test_explicit_uncertain_outcome_recovery_keeps_both_reservations(fake_api):
    root, pack, state = fake_api
    state['error'] = TimeoutError('Synthetic unknown response')
    with pytest.raises(TimeoutError):
        run(root, pack, service_tier='flex')
    ledger = root / 'runs/_api/ledger.json'
    before = json.loads(ledger.read_text())['entries']
    state['error'] = None
    result = run(root, pack, service_tier='flex', attempt_tag='manually-reviewed-double-reserve')
    after = json.loads(ledger.read_text())['entries']
    assert result['complete'] and result['budget']['unsettled'] == 1
    assert len(after) == 2 and state['generate'] == 2
    assert all(after[key] == value for key, value in before.items())
    assert run(root, pack, service_tier='flex')['new_answers'] == 0
    assert state['generate'] == 2
