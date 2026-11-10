"""Native Batch contracts with a fake SDK client; no network or provider credentials."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evaluation import answer
from test_evaluation import pack_fixture as pack_fixture

from cardforge.evaluate.api_budget import Ledger, token_cost
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.gemini_batch import BatchJudge


@pytest.fixture
def batch(pack_fixture, monkeypatch):
    genai = pytest.importorskip("google.genai")
    root, pack, _, _, public = pack_fixture

    def forbidden(*args, **kwargs):
        raise AssertionError("A real SDK client is forbidden in offline tests")

    monkeypatch.setattr(genai, "Client", forbidden)
    (root / "config").mkdir()
    (root / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=5.0\npaid_api_enabled=true\n"
    )
    (root / "config/evaluation.toml").write_text(
        '[gemini]\nmodel="gemini-3.8-flash"\ninput_usd_per_million=0.75\n'
        'output_usd_per_million=3.75\nprice_verified_date="2020-01-01"\n'
        'price_valid_through="2099-12-31"\nmax_input_tokens=20000\n'
    )
    registry = root / f'dataset/analysis/evaluation/keys/{public["pack_id"]}.json'
    key = json.loads(registry.read_text())
    first = public["items"][0]
    first["reference_context"] = [
        {"image": name, "role": role, "category": 1}
        for name, role in zip(first["refs"], ("style", "category"), strict=True)
    ]
    second = copy.deepcopy(first)
    second["item_id"] = "item-002"
    public["items"].append(second)
    key["mapping"][second["item_id"]] = copy.deepcopy(key["mapping"][first["item_id"]])
    key["mapping"][second["item_id"]]["item_id"] = "another-synthetic-source"
    public["run_settings"] = {
        "thinking_level": "LOW", "service_tier": "batch",
        "max_output_tokens": 1024, "max_total_generated_tokens": 2048,
    }
    public["rubric_version"] = "synthetic-batch-rubric"
    dump(pack / "pack.json", public)
    key["files"]["pack.json"] = sha256(pack / "pack.json")
    dump(registry, key)
    state = {"count_calls": 0, "create_calls": 0, "count": 1000,
             "create_error": None, "missing_name": False, "reservation_at_create": []}

    class FakeClient:
        def __init__(self):
            self.models = self.batches = self

        def count_tokens(self, **kwargs):
            assert kwargs["model"] == "gemini-3.8-flash"
            state["count_calls"] += 1
            return SimpleNamespace(total_tokens=state["count"])

        def create(self, *, model, src, config):
            assert model == "gemini-3.8-flash" and src
            state["create_calls"] += 1
            reserved = Ledger(root / "runs/_api/ledger.json", 5).summary()
            assert reserved["unsettled"] > 0  # A reservation exists before remote creation.
            state["reservation_at_create"].append(reserved)
            for request in src:
                assert request.config.max_output_tokens == 1024
                assert request.config.response_modalities == ["TEXT"]
                assert request.config.automatic_continuation is False
                assert request.config.automatic_function_calling.disable is True
            if state["create_error"]:
                raise state["create_error"]
            return SimpleNamespace(
                name=None if state["missing_name"] else f'batches/offline-{state["create_calls"]}'
            )

    client = FakeClient()
    judge = BatchJudge(root, pack, client)
    return judge, state, client


def response_entry(item: dict, **usage_override):
    usage = {"prompt_token_count": 1000, "candidates_token_count": 500,
             "thoughts_token_count": 100, "total_token_count": 1600, **usage_override}
    return SimpleNamespace(
        metadata={"key": item["item_id"]}, error=None,
        response=SimpleNamespace(
            text=json.dumps(answer(item)), model_version="gemini-3.8-flash",
            usage_metadata=SimpleNamespace(model_dump=lambda **kwargs: copy.deepcopy(usage)),
        ),
    )


def remote_job(job, entries, state="JOB_STATE_SUCCEEDED"):
    return SimpleNamespace(name=job["state"]["job_name"], state=SimpleNamespace(name=state),
                           dest=SimpleNamespace(inlined_responses=entries))


def submit_all(judge):
    return judge.submit([judge.prepare(identifier) for identifier in judge.items])


def test_batch_success_binds_reordered_entries_and_uses_half_rates_without_recount(batch):
    judge, state, _ = batch
    first_id = next(iter(judge.items))
    prepared = judge.prepare(first_id)
    assert judge.prepare(first_id) is prepared and state["count_calls"] == 1
    assert Decimal(prepared["ceiling"]) == token_cost(6096, 2048, 0.375, 1.875)
    job = submit_all(judge)
    entries = [response_entry(item) for item in reversed(list(judge.items.values()))]
    assert judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert judge.completed() == set(judge.items)
    assert Decimal(judge.ledger.summary()["accounted_upper_usd"]) == Decimal("0.003")
    assert judge.ledger.summary()["unsettled"] == 0
    assert judge.assemble()["n"] == 2
    assert judge.assemble()["complete"] and state["count_calls"] == 2
    assert state["create_calls"] == 1


def test_unknown_submission_keeps_full_reservation_and_cannot_be_reissued_after_restart(batch):
    judge, state, client = batch
    prepared = [judge.prepare(next(iter(judge.items)))]
    state["create_error"] = TimeoutError("Synthetic ambiguous creation")
    with pytest.raises(TimeoutError):
        judge.submit(prepared)
    job = json.loads(next(judge.jobs.glob("job-*.json")).read_text())
    assert job["state"] == "submission_unknown"
    before = judge.ledger.summary()
    assert before["unsettled"] == 1 and Decimal(before["accounted_upper_usd"]) > 0
    restarted = BatchJudge(judge.root, judge.pack, client)
    with pytest.raises(ValueError, match="assigned"):
        restarted.submit(prepared)
    assert restarted.ledger.summary() == before and state["create_calls"] == 1


def test_missing_remote_name_is_ambiguous_and_must_not_be_resubmitted(batch):
    judge, state, _ = batch
    state["missing_name"] = True
    prepared = [judge.prepare(next(iter(judge.items)))]
    with pytest.raises(ValueError, match="name missing"):
        judge.submit(prepared)
    assert judge.ledger.summary()["unsettled"] == 1
    with pytest.raises(ValueError, match="assigned"):
        judge.submit(prepared)
    assert state["create_calls"] == 1


def test_insufficient_shared_budget_and_duplicate_items_stop_before_create(batch):
    judge, state, _ = batch
    prepared = judge.prepare(next(iter(judge.items)))
    with pytest.raises(ValueError, match="Duplicate"):
        judge.submit([prepared, prepared])
    judge.ledger.reserve("other-project-call", Decimal("4.999"), {})
    with pytest.raises(ValueError, match="budget exhausted"):
        judge.submit([prepared])
    assert state["create_calls"] == 0
    # Even the fixed margin/output floor exceeds the stated remaining $0.827.
    assert 244 * token_cost(4096, 2048, 0.375, 1.875) == Decimal("1.311744")


@pytest.mark.parametrize("remote_state", ["JOB_STATE_PENDING", "JOB_STATE_RUNNING"])
def test_pending_job_retains_reserve_without_resubmission(batch, remote_state):
    judge, state, _ = batch
    job = submit_all(judge)
    before = judge.ledger.summary()
    assert not judge.collect(job["path"], job["state"], remote_job(job, [], remote_state))
    assert judge.ledger.summary() == before and state["create_calls"] == 1


def test_remote_job_name_mismatch_does_not_release_budget_or_cache_answers(batch):
    judge, _, _ = batch
    job = submit_all(judge)
    remote = remote_job(job, [response_entry(item) for item in judge.items.values()])
    remote.name = "batches/unrelated-job"
    before = judge.ledger.summary()
    with pytest.raises(ValueError, match="identity mismatch"):
        judge.collect(job["path"], job["state"], remote)
    assert judge.ledger.summary() == before and not judge.completed()


@pytest.mark.parametrize(
    "mutation", ["missing_key", "duplicate_key", "model", "unknown_model", "usage"]
)
def test_invalid_binding_or_usage_keeps_reserve_and_never_creates_parsed_answers(batch, mutation):
    judge, _, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    if mutation == "missing_key":
        entries[0].metadata = None
    elif mutation == "duplicate_key":
        entries[1].metadata = entries[0].metadata
    elif mutation == "model":
        entries[0].response.model_version = "another-model"
    elif mutation == "unknown_model":
        entries[0].response.model_version = None
    else:
        entries[0].response.usage_metadata = None
    before = judge.ledger.summary()
    with pytest.raises(ValueError):
        judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert judge.ledger.summary() == before and not judge.completed()


@pytest.mark.parametrize("mutation", ["malformed", "flat_json", "wrong_id", "sha"])
def test_invalid_answer_saves_every_receipt_settles_known_usage_and_promotes_nothing(
    batch, mutation
):
    judge, _, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    if mutation == "malformed":
        entries[0].response.text = '{"item_id":'
    elif mutation == "flat_json":
        entries[0].response.text = '{"style": true, "category": true}'
    elif mutation == "wrong_id":
        entries[0].response.text = entries[1].response.text
    else:
        data = json.loads(entries[0].response.text)
        data["image_sha256"] = "f" * 64
        entries[0].response.text = json.dumps(data)
    with pytest.raises(ValueError, match="response invalid"):
        judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert judge.ledger.summary()["unsettled"] == 0 and not judge.completed()
    assert Decimal(judge.ledger.summary()["accounted_upper_usd"]) == Decimal("0.003")
    for entry in entries:
        raw = json.loads((judge.output / f'{entry.metadata["key"]}.raw.json').read_text())
        assert raw["text"] == entry.response.text and raw["usage"]["prompt_token_count"] == 1000
    saved = json.loads(job["path"].read_text())
    assert saved["state"] == "response_invalid"
    assert saved["invalid_ids"] == [entries[0].metadata["key"]]


def test_assemble_rejects_spoofed_cached_request_id_without_counting_or_creating(batch):
    judge, state, _ = batch
    job = submit_all(judge)
    judge.collect(job["path"], job["state"], remote_job(
        job, [response_entry(item) for item in judge.items.values()]
    ))
    path = judge.output / f'{next(iter(judge.items))}.json'
    data = json.loads(path.read_text())
    data["request_id"] = "unrelated-frozen-request"
    dump(path, data)
    before = (state["count_calls"], state["create_calls"])
    with pytest.raises(ValueError, match="fingerprint"):
        judge.assemble()
    assert (state["count_calls"], state["create_calls"]) == before
    assert not (judge.pack / "answers.gemini.json").exists()


def test_changed_readme_is_rejected_before_counting_or_submitting(batch):
    judge, state, _ = batch
    (judge.pack / "README.txt").write_text("Changed unregistered instructions")
    with pytest.raises(ValueError, match="Changed"):
        judge.prepare(next(iter(judge.items)))
    assert state["count_calls"] == state["create_calls"] == 0


def test_sparse_job_history_never_overwrites_existing_operation(batch):
    judge, _, _ = batch
    dump(judge.jobs / "job-0001.json", {"items": [], "state": "collected"})
    existing = judge.jobs / "job-0003.json"
    dump(existing, {"items": [], "state": "submission_unknown"})
    before = existing.read_bytes()
    job = judge.submit([judge.prepare(next(iter(judge.items)))])
    assert Path(job["path"]).name == "job-0004.json" and existing.read_bytes() == before


def test_generated_tokens_include_total_usage_when_it_exceeds_candidate_plus_thoughts(batch):
    judge, _, _ = batch
    job = submit_all(judge)
    entries = [
        response_entry(item, candidates_token_count=10, thoughts_token_count=20,
                       total_token_count=2000)
        for item in judge.items.values()
    ]
    judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert Decimal(judge.ledger.summary()["accounted_upper_usd"]) == (
        2 * token_cost(1000, 1000, 0.375, 1.875)
    )


@pytest.mark.parametrize("remote_state", ["JOB_STATE_FAILED", "JOB_STATE_CANCELLED"])
def test_remote_failure_keeps_full_reservation_and_does_not_make_another_job(batch, remote_state):
    judge, state, _ = batch
    job = submit_all(judge)
    before = judge.ledger.summary()
    with pytest.raises(ValueError, match="did not succeed"):
        judge.collect(job["path"], job["state"], remote_job(job, [], remote_state))
    assert judge.ledger.summary() == before and state["create_calls"] == 1
    assert json.loads(job["path"].read_text())["state"] == "remote_failure"


def test_unrecognized_element_failure_retains_budget_and_does_not_invent_zero_cost(batch):
    judge, _, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    entries[0].error = SimpleNamespace(code=7)
    entries[0].response = None
    before = judge.ledger.summary()
    with pytest.raises(ValueError, match="Unknown batch failure billing"):
        judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert judge.ledger.summary() == before and not judge.completed()


def test_known_nonbillable_element_failure_is_recorded_without_reissuing_it(batch):
    judge, state, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    failed_id = entries[0].metadata["key"]
    entries[0].error = SimpleNamespace(code=503)
    entries[0].response = None
    assert judge.collect(job["path"], job["state"], remote_job(job, entries))
    summary = judge.ledger.summary()
    assert summary["unsettled"] == 0
    assert Decimal(summary["accounted_upper_usd"]) == Decimal("0.0015")
    assert json.loads(job["path"].read_text())["failed_ids"] == [failed_id]
    assert judge.completed() == set(judge.items) - {failed_id}
    assert state["create_calls"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        judge.assemble()


def test_assembly_requires_raw_receipt_to_match_cached_request(batch):
    judge, state, _ = batch
    job = submit_all(judge)
    judge.collect(job["path"], job["state"], remote_job(
        job, [response_entry(item) for item in judge.items.values()]
    ))
    raw = judge.output / f'{next(iter(judge.items))}.raw.json'
    receipt = json.loads(raw.read_text())
    receipt["request_id"] = "unrelated-raw-request"
    dump(raw, receipt)
    before = (state["count_calls"], state["create_calls"])
    with pytest.raises(ValueError):
        judge.assemble()
    assert (state["count_calls"], state["create_calls"]) == before
    assert not (judge.pack / "answers.gemini.json").exists()


def replace_schema_mode(judge, mode):
    public = json.loads((judge.pack / "pack.json").read_text())
    public["run_settings"]["schema_mode"] = mode
    dump(judge.pack / "pack.json", public)
    path = judge.root / f'dataset/analysis/evaluation/keys/{public["pack_id"]}.json'
    key = json.loads(path.read_text())
    key["files"]["pack.json"] = sha256(judge.pack / "pack.json")
    dump(path, key)


def test_native_schema_mode_changes_config_and_fingerprint_without_token_counting(batch):
    from google.genai import types

    judge, state, client = batch
    identifier = next(iter(judge.items))
    old_fingerprint = judge.request_fingerprint(identifier)
    replace_schema_mode(judge, "native-schema")
    native = BatchJudge(judge.root, judge.pack, client)
    assert isinstance(native.config.response_schema, types.Schema)
    assert native.config.response_json_schema is None
    assert native.request_fingerprint(identifier) != old_fingerprint
    assert native.config.response_schema.properties["fix_instruction"].nullable is True
    assert state["count_calls"] == state["create_calls"] == 0


def test_unknown_schema_mode_stops_before_count_or_create(batch):
    judge, state, client = batch
    replace_schema_mode(judge, "unsupported-schema-mode")
    with pytest.raises(ValueError, match="Unknown frozen batch schema mode"):
        BatchJudge(judge.root, judge.pack, client)
    assert state["count_calls"] == state["create_calls"] == 0


def test_native_sdk_wire_conversion_does_not_mutate_config_or_fingerprint(batch):
    from google.genai import models

    judge, state, client = batch
    replace_schema_mode(judge, "native-schema")
    native = BatchJudge(judge.root, judge.pack, client)
    identifier = next(iter(native.items))
    before_config = native.config.model_dump_json()
    before_fingerprint = native.request_fingerprint(identifier)
    # Pure SDK serialization only: no client construction or HTTP request.
    wire = models._GenerateContentConfig_to_mldev(None, native.config, parent_object={})
    assert "responseSchema" in wire and "responseJsonSchema" not in wire
    schema = wire["responseSchema"]
    properties = schema.properties if hasattr(schema, "properties") else schema["properties"]
    assert "criteria" in properties
    assert native.config.model_dump_json() == before_config
    assert native.request_fingerprint(identifier) == before_fingerprint
    assert state["count_calls"] == state["create_calls"] == 0


def test_all_invalid_answers_remain_unpromoted_after_idempotent_billing_collection(batch):
    judge, state, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    for entry in entries:
        entry.response.text = '{"style": true}'
    for _ in range(2):
        with pytest.raises(ValueError, match="response invalid"):
            judge.collect(job["path"], job["state"], remote_job(job, entries))
        assert judge.ledger.summary()["unsettled"] == 0 and not judge.completed()
        assert Decimal(judge.ledger.summary()["accounted_upper_usd"]) == Decimal("0.003")
    saved = json.loads(job["path"].read_text())
    assert set(saved["invalid_ids"]) == set(judge.items)
    assert len(list(judge.output.glob("*.raw.json"))) == 2
    assert state["create_calls"] == 1


def test_conflicting_error_and_billable_response_retains_reservation(batch):
    judge, _, _ = batch
    job = submit_all(judge)
    entries = [response_entry(item) for item in judge.items.values()]
    entries[0].error = SimpleNamespace(code=503)
    before = judge.ledger.summary()
    with pytest.raises(ValueError, match="Conflicting batch error/response"):
        judge.collect(job["path"], job["state"], remote_job(job, entries))
    assert judge.ledger.summary() == before and not judge.completed()


def test_frozen_high_thinking_is_preserved_and_changes_request_identity(batch):
    judge, state, client = batch
    identifier = next(iter(judge.items))
    old = judge.request_fingerprint(identifier)
    public = json.loads((judge.pack / 'pack.json').read_text())
    public['run_settings']['thinking_level'] = 'HIGH'
    dump(judge.pack / 'pack.json', public)
    path = judge.root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
    key = json.loads(path.read_text())
    key['files']['pack.json'] = sha256(judge.pack / 'pack.json')
    dump(path, key)
    high = BatchJudge(judge.root, judge.pack, client)
    assert high.config.thinking_config.thinking_level.value == 'HIGH'
    assert high.request_fingerprint(identifier) != old
    assert state['count_calls'] == state['create_calls'] == 0


def test_frozen_other_flash_profile_is_explicit_and_cannot_reuse_responses(batch):
    judge, state, client = batch
    identifier = next(iter(judge.items))
    before = judge.request_fingerprint(identifier)
    public = json.loads((judge.pack / 'pack.json').read_text())
    public['run_settings']['model'] = 'gemini-3.6-flash'
    dump(judge.pack / 'pack.json', public)
    path = judge.root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
    key = json.loads(path.read_text())
    key['files']['pack.json'] = sha256(judge.pack / 'pack.json')
    dump(path, key)
    alternate = BatchJudge(judge.root, judge.pack, client)
    assert alternate.cfg['model'] == 'gemini-3.6-flash'
    assert alternate.request_fingerprint(identifier) != before
    assert state['count_calls'] == state['create_calls'] == 0
