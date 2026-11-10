"""Stage 4 core: offline e2e on FakeBackend, resume, budget, gates, planner, prompts."""

import json
import os
import socket
import time
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from cardforge.cli import app
from cardforge.contracts.generation import PromptBundle
from cardforge.contracts.request import Budget, CardSpec, CollectionRequest, Hitl, PlanDraft
from cardforge.export.postprocess import finalize
from cardforge.llm.fake import fake_plan
from cardforge.llm.provider import HostAgentProvider, seal
from cardforge.orchestrator import service, states
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.execution import Limiter, TransientError, with_retries
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace, judge_profile, render_profile
from cardforge.planner.validators import load_grammar, load_vocabulary, validate_plans
from cardforge.policy.loader import resolve_active
from cardforge.policy.prompt_guard import PolicyPromptError
from cardforge.policy.report import PENDING_WARNING
from cardforge.prompting.assemble import build_bundle, load_template
from cardforge.render.fake import FakeBackend, SimulatedCrash

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "tests/snapshots/render_prompts"


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def brief(**kw) -> CollectionRequest:
    hitl = kw.pop("hitl", Hitl(g1="none", g2="none"))
    return CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme=kw.pop("set_theme", "Вестерн"),
        producer_wishes_raw=kw.pop("wishes", "побольше ретро"),
        render_profile="fake",
        hitl=hitl,
        **kw,
    )


def new_run(ws, request=None, *, judge="fake", llm="fake", panel=(), **options) -> str:
    """Runs without extra judges unless a test asks for a panel (keeps them fast)."""
    return service.create_run(
        ws,
        request or brief(),
        llm=llm,
        render="fake",
        judge=judge,
        shadow_judges=list(panel),
        run_id=options.pop("run_id", "r1"),
        runtime_options=options,
    )


def with_judges(monkeypatch, **changes) -> None:
    """Patch `[judges.*]` entries of config/runtime.toml for one test."""
    original = Workspace.config

    def patched(self):
        cfg = original(self)
        for name, update in changes.items():
            cfg["judges"][name].update(update)
        return cfg

    monkeypatch.setattr(Workspace, "config", patched)


def accepted_bundles(ws, rid) -> dict[str, str]:
    store = RunStore(ws.run_dir(rid))
    out = {}
    for cid, card in store.manifest().cards.items():
        if card.state == "accepted":
            rel = f"cards/{card.variant_id}/{cid}/attempt_{card.accepted_attempt:02d}/bundle.json"
            out[cid] = store.read_model(rel, PromptBundle).hash
    return out


# ---------------------------------------------------------------- e2e and demo


def test_demo_full_cycle_offline_under_a_minute(ws, no_network):
    started = time.monotonic()
    result = service.run_demo(ws, run_id="demo")
    elapsed = time.monotonic() - started
    assert elapsed < 60
    assert result["state"] == "completed", result
    assert result["cards"] == {"accepted": 30}
    run = ws.run_dir("demo")
    store = RunStore(run)
    manifest = store.manifest()
    assert set(manifest.ip_check) == {"v01", "v02", "v03"}
    assert all(c.status == "pending" for c in manifest.ip_check.values())
    report = (run / "report.md").read_text()
    for section in (
        "## Политики",
        "## Авторские права",
        "## Стоимость",
        "## Автономность",
        "## Судьи",
    ):
        assert section in report
    assert PENDING_WARNING in report
    assert "Принято с первой попытки: 28 из 30" in report
    assert (run / "report.html").read_text().startswith("<!doctype html>")
    for v in ("v01", "v02", "v03"):
        with Image.open(run / f"sheets/{v}.png") as sheet:
            assert sheet.size == (2150, 960)
        assert len(list((run / f"framed/{v}").glob("*.png"))) == 10
    finals = list(run.glob("cards/*/*/attempt_*/final.png"))
    assert len(finals) == 32  # 30 cards + one fix round + one flag regeneration
    for path in finals:
        with Image.open(path) as im:
            assert im.size == (430, 480) and im.mode == "RGB" and im.format == "PNG"
    template = load_template(ROOT).version
    for path in run.glob("cards/*/*/attempt_*/bundle.json"):
        bundle = PromptBundle.model_validate_json(path.read_text())
        assert bundle.template_version == template  # no hand-written render prompts
        assert bundle.hash == bundle.compute_hash()
    # Judge panel: every enabled test judge answered every attempt, each in its own file.
    assert manifest.shadow_judges == ["fake-strict", "fake-lenient"]
    for path in run.glob("cards/*/*/attempt_*/final.png"):
        judged = sorted(p.stem for p in (path.parent / "judges").glob("*.json"))
        assert judged == ["fake", "fake-lenient", "fake-strict"]
    events = store.events()
    assert {e.type for e in events} >= {
        "state_changed",
        "llm_call",
        "render_started",
        "render_finished",
        "check_result",
        "judge_result",
    }
    # Same seed, same plan and images: the demo is reproducible.
    again = service.run_demo(ws, run_id="demo2")
    assert again["state"] == "completed"
    assert accepted_bundles(ws, "demo") == accepted_bundles(ws, "demo2")


def test_cli_demo_and_status(ws, no_network):
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "--runs-dir", str(ws.runs_dir), "--run-id", "cli"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "completed"
    status = runner.invoke(app, ["status", str(ws.run_dir("cli"))])
    assert status.exit_code == 0 and json.loads(status.output)["cards"] == {"accepted": 30}
    export = runner.invoke(app, ["export", "cli", "--runs-dir", str(ws.runs_dir)])
    assert export.exit_code == 0 and "report.md" in export.output


# ---------------------------------------------------------------- resume and budget


def test_resume_does_not_regenerate_accepted_cards(ws, no_network):
    script = {"fake_judge": {"judge_script": {"v01_c02": ["fix", "accept"]}}}
    reference = new_run(ws, run_id="whole", **script)
    assert service.advance(ws, reference)["state"] == "completed"

    rid = new_run(ws, run_id="cut", **script)
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    runtime.backend = FakeBackend(crash_after=12)
    with pytest.raises(SimulatedCrash):
        service.advance(ws, rid, runtime)
    before = store.manifest()
    assert before.state == "rendering"
    accepted_before = {c for c, s in before.cards.items() if s.state == "accepted"}
    assert accepted_before, "the crash must happen after some cards were accepted"
    attempts_before = service.summarize_events(ws, rid)

    result = service.advance(ws, rid)  # what `cardforge resume` calls
    assert result["state"] == "completed"
    attempts_after = service.summarize_events(ws, rid)
    for cid in accepted_before:
        assert attempts_after[cid] == attempts_before[cid], cid
    assert accepted_bundles(ws, rid) == accepted_bundles(ws, reference)


def test_resume_reuses_ok_attempt_with_same_bundle(ws, monkeypatch):
    rid = new_run(ws)
    store = RunStore(ws.run_dir(rid))
    original = service._after_render

    def crash_before_qa(ctx, spec, rel):
        raise SimulatedCrash("killed after the image was saved")

    monkeypatch.setattr(service, "_after_render", crash_before_qa)
    with pytest.raises(SimulatedCrash):
        service.advance(ws, rid)
    monkeypatch.setattr(service, "_after_render", original)
    saved = {
        cid
        for cid, card in store.manifest().cards.items()
        if card.state == "rendering"
        and store.exists(f"cards/{card.variant_id}/{cid}/attempt_01/attempt.json")
    }
    assert saved
    before = service.summarize_events(ws, rid)
    runtime = service.build_runtime(ws, store.manifest())
    assert service.advance(ws, rid, runtime)["state"] == "completed"
    after = service.summarize_events(ws, rid)
    assert all(after[c] == before[c] for c in saved)  # reused, not regenerated
    assert runtime.backend.calls == 30 - len(saved)


def test_small_budget_ends_partial_without_extra_calls(ws, no_network):
    request = brief(budget=Budget(max_cost_usd=0.1))
    rid = new_run(ws, request, fake_backend={"cost_per_image_usd": 0.04})
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    backend = runtime.backend
    result = service.advance(ws, rid, runtime)
    assert result["state"] == "partial"
    assert result["reason"] == "budget_exhausted"
    assert backend.calls == 2  # 2 × $0.04 fit under $0.10; the third is never sent
    ledger = BudgetLedger(store, 0.1)
    assert ledger.committed() == pytest.approx(0.08)
    assert any(e.type == "budget_warning" for e in store.events())
    report = (ws.run_dir(rid) / "report.md").read_text()
    assert "`partial`" in report and "budget_exhausted" in report


def test_budget_reserve_and_settle(tmp_path):
    store = RunStore(tmp_path / "run")
    ledger = BudgetLedger(store, 0.1)
    rid = ledger.reserve(0.06, "render", "v01_c01")
    assert rid and ledger.reserve(0.06, "render", "v01_c02") is None
    ledger.settle(rid, 0.05)
    # Unsettled reservations survive a reload and keep counting.
    other = ledger.reserve(0.05, "render", "v01_c03")
    reloaded = BudgetLedger(store, 0.1)
    assert reloaded.committed() == pytest.approx(0.10)
    assert reloaded.totals()["unsettled"] == 1
    assert other


# ---------------------------------------------------------------- retries, blocks, kill switch


def test_transient_errors_retry_then_succeed(ws):
    rid = new_run(ws)
    runtime = service.build_runtime(ws, RunStore(ws.run_dir(rid)).manifest())
    runtime.backend = FakeBackend(transient_failures=3)
    runtime.concurrency = 1  # one card absorbs all three failures
    delays = []
    runtime.sleep = delays.append
    assert service.advance(ws, rid, runtime)["state"] == "completed"
    assert delays[:3] == [1.0, 2.0, 4.0]
    errors = [e for e in RunStore(ws.run_dir(rid)).events() if e.type == "error"]
    assert len(errors) == 3 and all(e.payload["kind"] == "transient" for e in errors)


def test_retries_exhausted_fail_the_card():
    calls = []

    def flaky():
        calls.append(1)
        raise TransientError("503", status=503)

    with pytest.raises(TransientError):
        with_retries(flaky, sleep=lambda s: None)
    assert len(calls) == 5  # first call + 4 retries (TA §15.1)


def test_retry_of_imageless_failure_keeps_the_attempt_budget(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")))
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    runtime.backend = FakeBackend(transient_failures=5)  # first call + 4 retries, no image
    runtime.concurrency = 1
    runtime.sleep = lambda s: None
    first = service.advance(ws, rid, runtime)
    failed = [c for c, s in store.manifest().cards.items() if s.state == "failed"]
    assert len(failed) == 1, first
    cid = failed[0]
    assert store.manifest().cards[cid].attempts == 1
    service.retry_failed(ws, rid, "test: cause fixed")
    card = store.manifest().cards[cid]
    assert card.attempts == 0 and card.state == "needs_fix"
    old = ws.run_dir(rid) / f"cards/{card.variant_id}/{cid}/attempt_01"
    assert (old / "attempt.failed-1.json").exists() and not (old / "attempt.json").exists()
    assert any(
        e.type == "error" and e.payload["kind"] == "attempt_released" for e in store.events()
    )
    if store.manifest().state in ("partial", "awaiting_g2"):
        service.reopen(ws, rid, "test: retry")
    runtime = service.build_runtime(ws, store.manifest())
    service.advance(ws, rid, runtime)
    card = store.manifest().cards[cid]
    assert card.attempts == 1 and (old / "raw.png").exists()


def test_provider_block_goes_to_g2_without_weakening_prompt(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")))
    runtime = service.build_runtime(ws, RunStore(ws.run_dir(rid)).manifest())
    runtime.backend = FakeBackend(blocked_objects=("Хлопушка",))
    result = service.advance(ws, rid, runtime)
    assert result["state"] == "awaiting_g2"
    assert result["waiting"] == ["v01_c01"]
    gate = json.loads((ws.run_dir(rid) / "review/g2.json").read_text())
    assert gate["items"][0]["reasons"] == ["blocked_by_provider"]


def test_kill_switch_stops_without_state_change(ws, monkeypatch):
    rid = new_run(ws)
    monkeypatch.setenv("CARDFORGE_STOP", "1")
    assert service.advance(ws, rid)["state"] == "created"
    monkeypatch.delenv("CARDFORGE_STOP")
    assert service.advance(ws, rid)["state"] == "completed"


def test_limiter_serializes_local_backends():
    assert Limiter(local=True, concurrency=4).concurrency == 1
    assert Limiter(local=False, concurrency=4).concurrency == 4


# ---------------------------------------------------------------- gates and judge profiles


def test_g1_and_g2_human_decisions(ws):
    request = brief(hitl=Hitl(g1="all", g2="flagged"))
    rid = new_run(ws, request, fake_judge={"judge_script": {"v01_c03": ["flag"]}})
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g1" and result["waiting"] == ["v01", "v02", "v03"]
    with pytest.raises(service.ServiceError):
        service.decide_gate(ws, rid, "g1", {"v01": "approve"}, reviewer=" ")
    service.decide_gate(
        ws, rid, "g1", {"v01": "approve", "v02": "approve", "v03": "reject"}, reviewer="panin"
    )
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g2" and result["waiting"] == ["v01_c03"]
    with pytest.raises(service.ServiceError):
        service.decide_gate(ws, rid, "g2", {"v01_c03": "fix:"}, reviewer="panin")
    service.decide_gate(ws, rid, "g2", {"v01_c03": "fix: поднять объект выше"}, reviewer="panin")
    result = service.advance(ws, rid)
    assert result["state"] == "completed", result
    store = RunStore(ws.run_dir(rid))
    manifest = store.manifest()
    assert {c.state for c in manifest.cards.values() if c.variant_id == "v03"} == {"rejected"}
    decisions = [e for e in store.events() if e.type == "human_decision"]
    assert [e.payload["gate"] for e in decisions] == ["G1", "G1", "G1", "G2"]
    g2 = decisions[-1].payload
    assert g2["judge_decision"] == "flag" and g2["agree_with_judge"] is True
    bundle = store.read_model("cards/v01/v01_c03/attempt_02/bundle.json", PromptBundle)
    first = store.read_model("cards/v01/v01_c03/attempt_01/bundle.json", PromptBundle)
    # Phase 1: the human comment is rewritten into English (FakeLLM here) before the
    # prompt; the original stays in the fix history and the events.
    assert "поднять объект выше" not in bundle.prompt_text
    assert "Apply the reviewer's crop correction" in bundle.prompt_text
    fix = manifest.counters["fix"]["v01_c03"]
    assert fix["entries"][0]["text"] == "поднять объект выше"
    assert fix["entries"][0]["inferred_codes"] == ["crop"]
    assert bundle.params.seed == first.params.seed  # a fix keeps the seed (TA §11.1)
    assert fix["retry"] == "same_seed" and fix["seed_sent"] is True
    # Gate decisions never touch the copyright status (G3, TA §9.8).
    assert all(c.status == "pending" for c in manifest.ip_check.values())
    report = (ws.run_dir(rid) / "report.md").read_text()
    assert "Вмешательств человека (G1/G2, G3 не считается): 4" in report


def test_g2_all_reviews_accepted_cards_too(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="all")))
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g2" and len(result["waiting"]) == 30
    decisions = {cid: "accept" for cid in result["waiting"]}
    decisions["v02_c04"] = "reject"
    service.decide_gate(ws, rid, "g2", decisions, reviewer="panin")
    result = service.advance(ws, rid)
    assert result["state"] == "partial" and result["reason"] == "incomplete_variants"
    events = RunStore(ws.run_dir(rid)).events()
    disagree = [
        e for e in events if e.type == "human_decision" and not e.payload["agree_with_judge"]
    ]
    assert [e.card_id for e in disagree] == ["v02_c04"]


def test_judge_none_requires_human_g2(ws):
    with pytest.raises(service.ServiceError, match="human G2"):
        new_run(ws, judge="none")
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none")
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g2" and len(result["waiting"]) == 30


def test_flag_in_gate_none_is_regenerated(ws):
    rid = new_run(ws, fake_judge={"judge_script": {"v03_c09": ["flag", "flag", "flag"]}})
    result = service.advance(ws, rid)
    card = RunStore(ws.run_dir(rid)).manifest().cards["v03_c09"]
    assert card.state == "rejected" and card.attempts == 3
    assert result["state"] == "partial" and result["reason"] == "incomplete_variants"


def test_judge_profiles_are_pluggable_and_none_is_default(ws):
    cfg = ws.config()
    assert cfg["defaults"]["judge_profile"] == "none"
    host = judge_profile(ws, "claude-v6")
    assert (host.provider, host.model, host.profile) == ("host-agent", "claude-opus-5-5", "v6")
    assert "host-agent:claude-code:claude-opus-5-5" in host.admitted
    from cardforge.evaluate.card_qa import judge_outcome

    answer = {
        "item_id": "v01_c01",
        "image_sha256": "0" * 64,
        "criteria": {
            k: {"passed": True, "note": "ok"}
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
        "style_uncertain": False,
        "issues": [],
        "fix_instruction": None,
    }
    assert judge_outcome(answer, "host-agent:claude-code", "claude-opus-5-5", host).decision == (
        "accept"
    )
    other = judge_outcome(answer, "host-agent:other", "model-x", host)
    assert other.decision == "flag" and "not an admitted judge" in other.issues[-1]


# ---------------------------------------------------------------- host agent (awaiting_agent)


def test_host_agent_waits_and_rejects_stale_answers(ws):
    rid = new_run(ws, llm="host-agent")
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_agent"
    assert result["pending_tasks"] == ["tasks/normalize.task.json"]
    run = ws.run_dir(rid)
    task = json.loads((run / "tasks/normalize.task.json").read_text())
    answer = {
        "task_sha256": "0" * 64,
        "provider": "host-agent:claude-code",
        "model": "claude-opus-5-5",
        "constraints": [],
        "preferences": ["побольше ретро"],
        "assumptions": [],
    }
    (run / "tasks/normalize.answer.json").write_text(json.dumps(answer))
    assert service.advance(ws, rid)["state"] == "awaiting_agent"  # stale answer is ignored
    answer["task_sha256"] = task["task_sha256"]
    (run / "tasks/normalize.answer.json").write_text(json.dumps(answer, ensure_ascii=False))
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_agent"
    assert result["pending_tasks"] == ["tasks/plan.task.json"]
    request = RunStore(run).read_model("request.json", CollectionRequest)
    assert request.preferences == ["побольше ретро"]

    def answer_task(name: str, payload: dict) -> None:
        sha = json.loads((run / f"tasks/{name}.task.json").read_text())["task_sha256"]
        body = {"task_sha256": sha, "provider": "host-agent:test", "model": "m", **payload}
        (run / f"tasks/{name}.answer.json").write_text(json.dumps(body, ensure_ascii=False))

    answer_task("plan", {"variants": [fake_plan(i, "Вестерн") for i in range(3)]})
    result = service.advance(ws, rid)
    assert result["pending_tasks"] == ["tasks/policy_screen.r0.task.json"]
    answer_task("policy_screen.r0", {"findings": []})
    result = service.advance(ws, rid)
    # Rendering ran; every card now waits for its PolicyInspector answer in one batch.
    assert result["state"] == "awaiting_agent"
    assert len(result["pending_tasks"]) == 30
    assert all(t.startswith("tasks/inspect.") for t in result["pending_tasks"])
    assert result["cards"] == {"evaluating": 30}


def test_host_agent_provider_needs_identity(tmp_path):
    store = RunStore(tmp_path / "r")
    task = seal({"step": "x", "a": 1})
    store.write("tasks/x.answer.json", {"task_sha256": task["task_sha256"], "provider": ""})
    with pytest.raises(ValueError, match="provider"):
        HostAgentProvider().ask(store, "x", task)


# ---------------------------------------------------------------- planner


def test_planner_repairs_invalid_plan(ws):
    rid = new_run(ws, fake_llm={"bad_plans": 1})
    assert service.plan(ws, rid)["state"] == "rendering"
    store = RunStore(ws.run_dir(rid))
    first = store.read_json("plan/rounds/round_0.json")
    assert any("repeated objects" in e for e in first["errors"])
    assert store.read_json("plan/validation.json")["rounds"] == 2


def test_planning_fails_after_two_repairs(ws):
    rid = new_run(ws, fake_llm={"bad_plans": 3})
    result = service.advance(ws, rid)
    assert result["state"] == "failed" and result["reason"] == "planning_failed"
    failed = RunStore(ws.run_dir(rid)).read_json("plan/planning_failed.json")
    assert failed["rounds"] == 3 and failed["unmet_rules"]


def test_screener_conflicts_and_policy_notes(ws):
    request = brief(wishes="как в Marvel, побольше ретро")
    rid = new_run(ws, request)
    service.plan(ws, rid)
    store = RunStore(ws.run_dir(rid))
    saved = store.read_model("request.json", CollectionRequest)
    assert any(c.rule_id and c.rule_id.startswith("IP-") for c in saved.conflicts)
    plans = store.read_json("plan/variants.json")
    for plan in plans:
        assert all(c.rule_id in " ".join(plan["policy_notes"]) for c in saved.conflicts)


def test_validators_catch_grammar_and_jaccard(ws):
    grammar, vocab = load_grammar(ROOT), load_vocabulary(ROOT)
    request = brief()
    plans = [PlanDraft.model_validate(fake_plan(i, "Вестерн")) for i in range(3)]
    assert validate_plans(plans, request, grammar, vocab)["valid"]
    clone = plans[0].model_copy()  # same id, same objects
    report = validate_plans([plans[0], clone, plans[2]], request, grammar, vocab)
    assert any("Jaccard" in e for e in report["errors"])
    assert any("variant ids repeat" in e for e in report["errors"])
    one_cat = plans[0].model_dump()
    for c in one_cat["cards"]:
        c.update(category=1, surface=None, environment=None)
    bad = PlanDraft.model_validate(one_cat)
    errors = validate_plans([bad, plans[1], plans[2]], request, grammar, vocab)["errors"]
    assert any("missing categories [2, 3, 4]" in e for e in errors)


# ---------------------------------------------------------------- prompts and postprocess


def _spec() -> CardSpec:
    draft = fake_plan(0, "Вестерн")["cards"][2]
    return CardSpec(
        **draft,
        card_id="v01_c03",
        safe_zone={"top_pct": 0.14, "bottom_pct": 0.11, "side_pct": 0.08},
        reference_roles={},
        style_profile_id="reference_cards_v1",
        seed=7,
    )


@pytest.mark.parametrize("profile", ["fake", "closed-nano", "local-klein4b"])
def test_render_prompt_snapshots(ws, profile):
    from cardforge.contracts.generation import Reference

    chosen = render_profile(ws, profile)
    template = load_template(ROOT, chosen.template)
    packs = resolve_active(ROOT, ["CN"], [])
    refs = [
        Reference(id="CardC6_card_17", role="style", path="dataset/CardC6_card_17.png"),
        Reference(id="CardC6_card_06", role="category", path="dataset/CardC6_card_06.png"),
    ]
    bundle = build_bundle(
        template,
        chosen,
        _spec(),
        packs,
        [],
        seed=7,
        references=refs,
        max_refs=6,
        fix_instruction="Поднять объект выше.",
    )
    snapshot = SNAPSHOTS / f"{profile}.txt"
    text = f"template: {template.version}\nrefs: {[r.id for r in bundle.references]}\n\n"
    text += bundle.prompt_text + "\n"
    if os.environ.get("CARDFORGE_UPDATE_SNAPSHOTS") == "1":
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(text, encoding="utf-8")
    assert snapshot.read_text(encoding="utf-8") == text
    assert bundle.hash == bundle.compute_hash()


def test_prompt_guard_blocks_forbidden_fix_instruction(ws):
    packs = resolve_active(ROOT, [], [])
    with pytest.raises(PolicyPromptError):
        build_bundle(
            load_template(ROOT),
            render_profile(ws, "fake"),
            _spec(),
            packs,
            [],
            seed=1,
            references=[],
            max_refs=6,
            fix_instruction="сделать как у Marvel",
        )


@pytest.mark.parametrize("size", [(864, 960), (1024, 1024), (1200, 900)])
def test_postprocess_crops_to_card_size(size):
    import io

    from cardforge.contracts.request import SafeZone

    image = Image.new("RGB", size, (200, 220, 240))
    Image.Image.paste(image, (30, 30, 30), (size[0] // 3, size[1] // 3, size[0] // 2, size[1] // 2))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data, info = finalize(buf.getvalue(), SafeZone(top_pct=0.14, bottom_pct=0.11, side_pct=0.08))
    with Image.open(io.BytesIO(data)) as out:
        assert out.size == (430, 480) and out.mode == "RGB"
    left, top, right, bottom = info["crop"]
    assert abs((right - left) / (bottom - top) - 430 / 480) < 0.01
    assert info["object_box_estimate"] is not None


# ---------------------------------------------------------------- store and state machine


def test_store_writes_are_atomic_and_states_checked(tmp_path):
    store = RunStore.create(tmp_path / "r1")
    store.write("plan/x.json", {"a": 1})
    assert not [p for p in (tmp_path / "r1").rglob(".*") if p.is_file()]
    with pytest.raises(states.TransitionError):
        states.set_run_state(store, "exported")
    states.register_cards(store, {"v01_c01": "v01"})
    with pytest.raises(states.TransitionError):
        states.set_card_state(store, "v01_c01", "accepted")
    states.set_card_state(store, "v01_c01", "spec_ready")
    lines = (tmp_path / "r1/events.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["payload"] == {
        "from": "planned",
        "to": "spec_ready",
        "reason": None,
    }


def test_demo_profiles_are_marked_test_only(ws):
    assert render_profile(ws, "fake").test_only
    assert not render_profile(ws, "closed-nano").enabled
    with pytest.raises(service.ServiceError, match="switched off"):
        service.create_run(ws, brief(), llm="fake", render="closed-nano", judge="fake")


def test_fake_backend_dataset_mode_uses_category_reference(tmp_path):
    from cardforge.contracts.generation import Reference, RenderParams

    ref = tmp_path / "ref.png"
    Image.new("RGB", (430, 480), (10, 200, 30)).save(ref)
    bundle = PromptBundle(
        backend="fake",
        model_id="fake",
        prompt_text="x",
        references=[Reference(id="r", role="category", path=str(ref))],
        params=RenderParams(width=864, height=960, seed=1),
        template_version="t",
    ).sealed()
    import io

    result = FakeBackend(source="dataset").render(bundle, 10)
    with Image.open(io.BytesIO(result.image_png)) as im:
        assert im.size == (864, 960) and im.getpixel((5, 5)) == (10, 200, 30)
    missing = bundle.model_copy(update={"references": []}).sealed()
    with pytest.raises(FileNotFoundError):
        FakeBackend(source="dataset").render(missing, 10)
    assert FakeBackend(source="auto").render(missing, 10).status == "ok"


# ---------------------------------------------------------------- judge panel


def test_judge_panel_logs_shadow_answers_without_changing_decisions(ws, monkeypatch):
    with_judges(monkeypatch, **{"claude-v6": {"enabled": True}})
    rid = new_run(
        ws,
        panel=["fake-strict", "claude-v6"],
        run_id="panel",
        fake_judges={"fake-strict": {"judge_script": {"v01_c01": ["reject"]}}},
    )
    result = service.advance(ws, rid)
    # The host-agent judge has not answered, yet it does not hold the run.
    assert result["state"] == "completed" and result["cards"] == {"accepted": 30}
    store = RunStore(ws.run_dir(rid))
    attempt = "cards/v01/v01_c01/attempt_01"
    record = store.read_json(f"{attempt}/judges/fake-strict.json")
    assert record["role"] == "shadow" and record["profile"] == "rubric7-runtime-v1"
    assert record["model"] == "fake-judge-b" and record["raw_answer"]["criteria"]
    assert record["outcome"]["decision"] == "reject"
    ev = store.read_json(f"{attempt}/eval.json")
    assert ev["decision"] == "accept" and ev["judge"]["judge_model"] == "fake:fake-judge-a"
    assert ev["shadow_judges"]["claude-v6"].startswith("pending: tasks/judge.claude-v6.")
    assert not store.exists(f"{attempt}/judges/claude-v6.json")
    report = (ws.run_dir(rid) / "report.md").read_text()
    decisions = [
        store.read_json(f"cards/{c[:3]}/{c}/attempt_01/judges/{j}.json")["outcome"]["decision"]
        for c in sorted(store.manifest().cards)
        for j in ("fake", "fake-strict")
    ]
    same = sum(a == b for a, b in zip(decisions[::2], decisions[1::2]))
    assert same < 30  # the scripted reject at least
    assert f"| `fake` / `fake-strict` | 30 | {same} ({same / 30:.0%}) |" in report


def test_judge_answers_are_written_once(ws, monkeypatch):
    with_judges(monkeypatch, **{"claude-v6": {"enabled": True}})
    rid = new_run(ws, judge="claude-v6", panel=["fake-strict"], run_id="once")
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_agent"  # the deciding judge is a host agent
    run = ws.run_dir(rid)
    files = sorted(run.glob("cards/*/*/attempt_01/judges/fake-strict.json"))
    assert len(files) == 30
    stamps = {p: p.stat().st_mtime_ns for p in files}
    assert service.advance(ws, rid)["state"] == "awaiting_agent"
    assert {p: p.stat().st_mtime_ns for p in files} == stamps
    calls = [
        e
        for e in RunStore(run).events()
        if e.type == "llm_call" and e.payload["key"].startswith("judge.fake-strict.")
    ]
    assert len(calls) == 30  # not asked again on resume


def test_judge_has_its_own_budget(ws, monkeypatch):
    with_judges(monkeypatch, **{"fake-strict": {"max_cost_usd": 0.03}})
    request = brief(budget=Budget(max_cost_usd=1.0))
    rid = new_run(
        ws,
        request,
        panel=["fake-strict"],
        fake_judges={"fake-strict": {"cost_per_call_usd": 0.01}},
    )
    assert service.advance(ws, rid)["state"] == "completed"
    store = RunStore(ws.run_dir(rid))
    assert len(list(ws.run_dir(rid).glob("cards/*/*/attempt_*/judges/fake-strict.json"))) == 3
    assert BudgetLedger(store, 1.0).committed("judge:fake-strict") == pytest.approx(0.03)
    warnings = [e for e in store.events() if e.type == "budget_warning"]
    assert warnings and warnings[0].payload["reason"] == "judge_budget_exhausted"


def test_judge_switched_off_in_config_leaves_the_panel(ws, monkeypatch):
    rid = new_run(ws, panel=["fake-strict", "fake-lenient"], run_id="off")
    with_judges(monkeypatch, **{"fake-lenient": {"enabled": False}})
    assert service.default_panel(ws, "fake", test_run=True) == ["fake-strict"]
    assert service.advance(ws, rid)["state"] == "completed"
    run = ws.run_dir(rid)
    assert not list(run.glob("cards/*/*/attempt_*/judges/fake-lenient.json"))
    assert "Выключены в конфиге после старта: `fake-lenient`" in (run / "report.md").read_text()
    # Fake judges never join a run with a real render profile.
    assert service.default_panel(ws, "none", test_run=False) == []


def test_judge_panel_switch_off_and_validation(ws, monkeypatch):
    with pytest.raises(service.ServiceError, match="switched off"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           shadow_judges=["claude-v6"])  # fmt: skip
    with pytest.raises(service.ServiceError, match="deciding judge"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           shadow_judges=["fake"])  # fmt: skip
    with pytest.raises(ValueError, match="unknown judge"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           shadow_judges=["nobody"])  # fmt: skip
    for name in ("gemini-flash", "claude-v6"):
        assert not judge_profile(ws, name).enabled_in_config
    with pytest.raises(service.ServiceError, match="needs candidate mode"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           shadow_judges=["fake-sol"])  # fmt: skip
    with_judges(monkeypatch, **{"gemini-flash": {"enabled": True}})
    with pytest.raises(service.ServiceError, match="no adapter"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           shadow_judges=["gemini-flash"])  # fmt: skip


# ---------------------------------------------------------------- candidate mode, two judges


def group_run(ws, request=None, **options) -> str:
    return service.create_run(
        ws,
        request or brief(hitl=Hitl(g1="none", g2="flagged")),
        llm="fake",
        render=options.pop("render", "fake"),
        deciders=["fake-g1", "fake-sol"],
        shadow_judges=[],
        run_id=options.pop("run_id", "g1"),
        runtime_options=options,
    )


def test_candidate_mode_two_deciding_judges(ws, no_network):
    picks = {"v01_c01.a01": 2, "v01_c02.a01": 3, "v01_c03.a01": None}
    sol = {
        "v01_c01.a01": ["reject", "accept", "reject", "reject"],  # pick confirmed → accept
        "v01_c02.a01": ["accept", "reject", "reject", "reject"],  # disagree → G2
        "v01_c03.a01": ["fix", "fix", "fix", "fix"],  # neither → regenerate
    }
    rid = group_run(
        ws,
        fake_judges={
            "fake-g1": {"script": picks},
            "fake-sol": {"script": sol, "fix_rate": 0},
        },
    )
    manifest = RunStore(ws.run_dir(rid)).manifest()
    assert manifest.candidates_per_card == 4 and manifest.decision_rule == "pick-and-confirm"
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g2" and result["waiting"] == ["v01_c02"]
    store = RunStore(ws.run_dir(rid))
    cards = store.manifest().cards
    assert cards["v01_c01"].state == "accepted" and cards["v01_c01"].accepted_candidate == 2
    assert cards["v01_c03"].attempts == 2  # regenerated as a whole new group
    for k in range(1, 5):
        assert store.exists(f"cards/v01/v01_c01/attempt_01/cand_{k}/final.png")
        assert store.exists(f"cards/v01/v01_c01/attempt_01/cand_{k}/judges/fake-g1.json")
        assert store.exists(f"cards/v01/v01_c01/attempt_01/cand_{k}/judges/fake-sol.json")
    item = store.read_json("review/g2.json")["items"][0]
    assert item["candidates"]["3"]["verdicts"] == {"fake-g1": "accept", "fake-sol": "reject"}
    assert item["candidates"]["1"]["verdicts"] == {"fake-g1": "reject", "fake-sol": "accept"}
    service.decide_gate(ws, rid, "g2", {"v01_c02": "accept: 1"}, reviewer="panin")
    result = service.advance(ws, rid)
    assert result["state"] == "completed", result
    cards = RunStore(ws.run_dir(rid)).manifest().cards
    assert cards["v01_c02"].accepted_candidate == 1
    report = (ws.run_dir(rid) / "report.md").read_text()
    assert "## Решающие судьи" in report and "pick-and-confirm" in report
    assert "v01_c02 attempt_01: fake-g1 принимает" in report
    with Image.open(ws.run_dir(rid) / "sheets/v01.png") as sheet:
        assert sheet.size == (2150, 960)


def test_candidate_mode_resume_reuses_images_and_judge_rounds(ws, monkeypatch):
    rid = group_run(ws, brief(hitl=Hitl(g1="none", g2="none")), run_id="gr")
    calls = {"n": 0}
    original = service._evaluate_round

    def crash_once(ctx, specs):
        if calls["n"] == 0 and any(c.state == "evaluating" for c in ctx.manifest.cards.values()):
            calls["n"] += 1
            raise SimulatedCrash("killed before judging")
        return original(ctx, specs)

    monkeypatch.setattr(service, "_evaluate_round", crash_once)
    with pytest.raises(SimulatedCrash):
        service.advance(ws, rid)

    def first_attempt_renders() -> int:
        return sum(
            e.type == "render_started" and e.payload["attempt_no"] == 1
            for e in RunStore(ws.run_dir(rid)).events()
        )

    before = first_attempt_renders()
    assert before == 120
    runtime = service.build_runtime(ws, RunStore(ws.run_dir(rid)).manifest())
    assert service.advance(ws, rid, runtime)["state"] in ("completed", "partial")
    # Every image rendered before the crash is reused, not rendered again.
    assert first_attempt_renders() == before
    first_round = RunStore(ws.run_dir(rid)).read_json("judging/round_01/items.json")
    assert len(first_round) == 120  # 30 positions x 4 candidates in one round


def test_candidate_mode_needs_calibrated_n_and_batch_judges(ws):
    with pytest.raises(service.ServiceError, match="uncalibrated"):
        service.create_run(ws, brief(), llm="fake", render="fake",
                           deciders=["fake-g1", "fake-sol"], candidates=3)  # fmt: skip
    with pytest.raises(service.ServiceError, match="batch judges"):
        service.create_run(ws, brief(), llm="fake", render="fake", deciders=["fake", "fake-sol"])
    # Kimi G1 spends the OpenRouter ledger: a run budget is mandatory and capped by it.
    with pytest.raises(service.ServiceError, match="OpenRouter ledger"):
        service.create_run(ws, brief(), llm="fake", render="fake",
                           deciders=["kimi-g1", "fake-sol"])  # fmt: skip


def test_several_render_models_one_per_variant(ws):
    rid = service.create_run(
        ws, brief(), llm="fake", render=["fake", "fake-b"], judge="fake", shadow_judges=[],
        run_id="models",
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "completed"
    store = RunStore(ws.run_dir(rid))
    assert store.manifest().variant_profiles == {"v01": "fake", "v02": "fake-b", "v03": "fake"}
    for variant, model in (("v01", "fake-backend-v1"), ("v02", "fake-backend-v1b")):
        for path in ws.run_dir(rid).glob(f"cards/{variant}/*/attempt_*/bundle.json"):
            assert PromptBundle.model_validate_json(path.read_text()).model_id == model
    report = (ws.run_dir(rid) / "report.md").read_text()
    assert "## Модели" in report and "| `fake-b` | fake-backend-v1b | v02 | 10 из 10 |" in report
    with pytest.raises(service.ServiceError, match="switched off"):
        service.create_run(ws, brief(), llm="fake", render=["fake", "closed-nano"], judge="fake")


def test_pick_and_confirm_rule():
    from cardforge.evaluate.batch_judges import JudgeItem, pick_and_confirm

    members = [JudgeItem(f"c.a01.k{k}", "c", "c.a01", k, "x", "0", {}) for k in (1, 2)]
    ids = {m.item_id for m in members}
    yes, no = {"accept": True}, {"accept": False}
    both = pick_and_confirm({"c.a01.k2": yes}, {"c.a01.k2": yes}, members, ids)
    assert both["decision"] == "accept" and both["item_id"] == "c.a01.k2"
    split = pick_and_confirm({"c.a01.k2": yes}, {"c.a01.k1": yes, "c.a01.k2": no}, members, ids)
    assert split["decision"] == "flag" and split["confirmer_accepts"] == ["c.a01.k1"]
    assert pick_and_confirm({}, {}, members, ids)["decision"] == "fix"
    # A candidate that failed hard checks is never accepted, whatever the judges say.
    assert (
        pick_and_confirm({"c.a01.k2": yes}, {"c.a01.k2": yes}, members, {"c.a01.k1"})["decision"]
        == "fix"
    )


def test_render_variant_limit_renders_only_first_variants(ws):
    rid = service.create_run(
        ws, brief(), llm="fake", render="fake", judge="fake", shadow_judges=[], run_id="lim",
        render_variant_limit=1,
    )  # fmt: skip
    result = service.advance(ws, rid)
    assert result["state"] == "completed" and result["cards"] == {"accepted": 10, "rejected": 20}
    store = RunStore(ws.run_dir(rid))
    assert not list(ws.run_dir(rid).glob("cards/v02/*")) and not list(
        ws.run_dir(rid).glob("cards/v03/*")
    )
    assert store.manifest().cards["v02_c01"].reason.startswith("not rendered")
    assert "Тестовый лимит пользователя" in (ws.run_dir(rid) / "report.md").read_text()
    with pytest.raises(service.ServiceError, match="render_variant_limit"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           render_variant_limit=4)  # fmt: skip


def test_screener_sees_english_object_description():
    from cardforge.policy.screener import screen_plan

    draft = fake_plan(0, "Вестерн")
    draft["cards"][0]["object_description"] = "Marvel style hero clapperboard"
    verdicts = screen_plan([PlanDraft.model_validate(draft)], brief(), resolve_active(ROOT, [], []))
    assert any(v.target == "v01_c01" and v.rule_id == "IP-BRD-01" for v in verdicts)


def test_codex_judge_unavailable_raises(tmp_path, monkeypatch):
    """Out of subscription credits = no answers: the deciding judge must fail loudly."""
    from cardforge.evaluate import batch_judges

    judge = batch_judges.CodexCliJudge(ROOT)
    folder = tmp_path / "codex-sol"
    (folder / "cli-runs/1").mkdir(parents=True)
    (folder / "cli-runs/1/stream.jsonl").write_text(
        '{"type":"error","message":"Your workspace is out of credits."}\n'
    )
    assert "out of credits" in judge._last_cli_error(folder)


def test_openrouter_image_backend_offline(tmp_path, monkeypatch):
    import base64
    import io
    import json as _json

    import httpx

    from cardforge.contracts.generation import RenderParams
    from cardforge.evaluate.api_budget import Ledger
    from cardforge.orchestrator.execution import TransientError
    from cardforge.render.openrouter_image import OpenRouterImageBackend

    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), (90, 120, 200)).save(buf, format="PNG")
    png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    replies = []

    def handler(request):
        body = _json.loads(request.content)
        replies.append(body)
        mode = body["messages"][0]["content"][0]["text"]
        if mode == "rate":
            return httpx.Response(429, json={"error": "rate"})
        if mode == "blocked":
            choice = {"finish_reason": "content_filter", "message": {"content": "refused"}}
            return httpx.Response(200, json={"choices": [choice], "usage": {"cost": 0.0}})
        message = {"images": [{"type": "image_url", "image_url": {"url": png}}]}
        return httpx.Response(200, json={"id": "gen-1", "model": body["model"],
                                         "choices": [{"message": message}],
                                         "usage": {"cost": 0.04}})  # fmt: skip

    ledger = tmp_path / "ledger.json"
    backend = OpenRouterImageBackend(
        ROOT, "google/gemini-nano-banana-2.1", estimate_usd=0.05, ledger_path=ledger,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip

    def bundle(text):
        return PromptBundle(backend="openrouter-image", model_id="m", prompt_text=text,
                            params=RenderParams(width=864, height=960, seed=7),
                            template_version="t").sealed()  # fmt: skip

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    ok = backend.render(bundle("draw"), 30)
    assert ok.status == "ok" and ok.cost_usd == pytest.approx(0.04)
    with Image.open(io.BytesIO(ok.image_png)) as im:
        assert im.size == (800, 1000)
    sent = replies[0]
    assert sent["provider"]["allow_fallbacks"] is False
    assert sent["provider"]["data_collection"] == "deny" and "openai" in sent["provider"]["ignore"]
    assert sent["modalities"] == ["image", "text"] and len(sent["messages"][0]["content"]) == 1
    with pytest.raises(TransientError):
        backend.render(bundle("rate"), 30)
    assert backend.render(bundle("blocked"), 30).status == "blocked_by_provider"
    import tomllib

    cap = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]["budget_usd"]
    summary = Ledger(ledger, cap, cap).summary()
    assert Decimal(summary["accounted_upper_usd"]) == Decimal("0.04")
    with pytest.raises(ValueError, match="OpenAI"):
        OpenRouterImageBackend(ROOT, "openai/gpt-image-2", estimate_usd=0.05)


def test_compare_models_draws_the_same_variant_per_model(ws):
    request = brief(hitl=Hitl(g1="none", g2="flagged"))
    rid = service.create_run(
        ws, request, llm="fake", render=["fake", "fake-b"], judge="fake", shadow_judges=[],
        run_id="cmp", compare_models=True,
    )  # fmt: skip
    result = service.advance(ws, rid)
    assert result["state"] in ("completed", "awaiting_g2"), result
    store = RunStore(ws.run_dir(rid))
    m = store.manifest()
    assert m.compare_variants == ["v01", "v01m2"]
    assert m.variant_profiles["v01"] == "fake" and m.variant_profiles["v01m2"] == "fake-b"
    plans = {p["variant_id"]: p for p in store.read_json("plan/variants.json")}
    names = lambda v: [c["object_name"] for c in plans[v]["cards"]]  # noqa: E731
    assert names("v01") == names("v01m2")
    rendered = {p.name for p in ws.run_dir(rid).glob("cards/*/*")}
    assert rendered == {f"v01_c{n:02d}" for n in range(1, 11)} | {
        f"v01m2_c{n:02d}" for n in range(1, 11)
    }
    with pytest.raises(service.ServiceError, match="two render profiles"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake",
                           compare_models=True)  # fmt: skip


def test_codex_image_backend_reports_provider_limit(monkeypatch):
    import subprocess

    from cardforge.contracts.generation import RenderParams
    from cardforge.render.codex_image import CodexImageBackend

    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        out = '{"type":"error","message":"Your workspace is out of credits."}\n'
        return subprocess.CompletedProcess(argv, 1, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = CodexImageBackend(ROOT, "gpt-6.1-terra")
    bundle = PromptBundle(backend="codex", model_id="m", prompt_text="draw a clapperboard",
                          params=RenderParams(width=864, height=960, seed=1),
                          template_version="t").sealed()  # fmt: skip
    result = backend.render(bundle, 60)
    assert result.status == "error" and result.error.startswith("provider_limit:")
    argv, kw = calls[0]
    assert argv[argv.index("-m") + 1] == "gpt-6.1-terra"
    assert "OPENAI_API_KEY" not in kw["env"] and "built-in image generation" in kw["input"]
    assert backend.capabilities().local  # strictly sequential


def test_openrouter_images_endpoint_and_int32_seed(tmp_path, monkeypatch):
    import base64
    import io
    import json as _json

    import httpx

    from cardforge.contracts.generation import RenderParams
    from cardforge.render.openrouter_image import OpenRouterImageBackend

    buf = io.BytesIO()
    Image.new("RGB", (64, 80), (1, 2, 3)).save(buf, format="PNG")
    seen = []

    def handler(request):
        seen.append((request.url.path, _json.loads(request.content)))
        item = {"b64_json": base64.b64encode(buf.getvalue()).decode()}
        return httpx.Response(200, json={"data": [item], "usage": {"cost": 0.048}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    backend = OpenRouterImageBackend(
        ROOT, "black-forest-labs/flux-3-image", estimate_usd=0.05, endpoint="images",
        ledger_path=tmp_path / "ledger.json", transport=httpx.MockTransport(handler),
    )  # fmt: skip
    bundle = PromptBundle(backend="openrouter-image", model_id="m", prompt_text="draw",
                          params=RenderParams(width=864, height=960, seed=4039189304),
                          template_version="t").sealed()  # fmt: skip
    result = backend.render(bundle, 30)
    assert result.status == "ok" and result.cost_usd == pytest.approx(0.048)
    path, body = seen[0]
    assert path.endswith("/images") and body["prompt"] == "draw" and body["seed"] < 2**31


def test_ref_template_labels_images_and_background(ws):
    from cardforge.contracts.generation import Reference

    profile = render_profile(ws, "or-gpt-image-2")
    refs = [
        Reference(id="CardC6_card_17", role="style", path="dataset/CardC6_card_17.png"),
        Reference(id="CardC6_card_47", role="background", path="x/CardC6_card_47.png"),
        Reference(id="CardC6_card_06", role="category", path="dataset/CardC6_card_06.png"),
    ]
    from cardforge.contracts.request import Background

    spec = _spec().model_copy(update={"background": Background(motif="rays", color="orange")})
    bundle = build_bundle(
        load_template(ROOT, profile.template), profile, spec, resolve_active(ROOT, [], []), [],
        seed=1, references=refs, max_refs=2,
        motif_texts={"rays": "two-tone rays, lighter centre"},
    )  # fmt: skip
    assert [r.role for r in bundle.references] == ["style", "background"]
    assert "Image 1: style reference" in bundle.prompt_text
    assert "Image 2: background reference" in bundle.prompt_text
    assert "orange backdrop" in bundle.prompt_text and "two-tone rays" in bundle.prompt_text
    assert "3 to 6 large readable shapes" in bundle.prompt_text
    # Rules from the human G2 review of kino-western-03 (ref-v2).
    for rule in (
        "at most one or two",
        "continues naturally",
        "never downward",
        "nothing is cut off",
        "clearly contrasting with it",
        # ref-v3: second G2 round.
        "stays inside this safe zone",
        "Never repeat an element",
    ):
        assert rule in bundle.prompt_text, rule
    for category in (2, 3):
        standing = spec.model_copy(update={"category": category})
        text = build_bundle(
            load_template(ROOT, profile.template), profile, standing,
            resolve_active(ROOT, [], []), [], seed=1, references=refs, max_refs=2,
            motif_texts={"rays": "two-tone rays, lighter centre"},
        ).prompt_text  # fmt: skip
        assert "one straight line" in text and "three bands" in text


def test_openai_image_model_needs_explicit_profile_flag(tmp_path, monkeypatch):
    import json as _json

    import httpx

    from cardforge.contracts.generation import Reference, RenderParams
    from cardforge.render.openrouter_image import OpenRouterImageBackend

    seen = []

    def handler(request):
        seen.append(_json.loads(request.content))
        return httpx.Response(400, json={"error": "stop here"})

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    with pytest.raises(ValueError, match="OpenAI"):
        OpenRouterImageBackend(ROOT, "openai/gpt-image-2", estimate_usd=0.08)
    ref = tmp_path / "ref.png"
    Image.new("RGB", (8, 8)).save(ref)
    backend = OpenRouterImageBackend(
        tmp_path, "openai/gpt-image-2", estimate_usd=0.08, endpoint="images", allow_openai=True,
        send_references=True, send_seed=False, extra={"quality": "medium"},
        ledger_path=tmp_path / "ledger.json", transport=httpx.MockTransport(handler),
    )  # fmt: skip
    backend.root = ROOT  # config lives in the project; the reference path is absolute
    bundle = PromptBundle(backend="openrouter-image", model_id="m", prompt_text="draw",
                          references=[Reference(id="r", role="style", path=str(ref))],
                          params=RenderParams(width=864, height=960, seed=1),
                          template_version="t").sealed()  # fmt: skip
    assert backend.render(bundle, 30).status == "error"
    body = seen[0]
    assert "openai" not in body["provider"]["ignore"] and body["quality"] == "medium"
    first = body["input_references"][0]
    assert first["type"] == "image_url" and "seed" not in body
    assert first["image_url"]["url"].startswith("data:image/png;base64,")


def test_review_page_and_import_round_trip(ws):
    from cardforge.export.review_page import write_review_page

    request = brief(hitl=Hitl(g1="none", g2="flagged"))
    rid = service.create_run(
        ws, request, llm="fake", render=["fake", "fake-b"], judge="none", shadow_judges=[],
        run_id="page", compare_models=True,
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    page = write_review_page(store, blind=False).read_text()  # the operator's open page
    data = json.loads(page[page.index("const DATA = ") + 13 : page.index(";\nconst KEY")])
    assert [r["variant_id"] for r in data["rows"]] == ["v01", "v01m2"]
    cells = [c for r in data["rows"] for c in r["cells"]]
    assert len(cells) == 20 and all(c["image"].startswith("data:image/jpeg;base64,") for c in cells)
    # The JSON the page downloads (see review_page.html, result()).
    decisions = {c["card_id"]: "reject" for c in cells}
    decisions["v01_c01"] = "accept"
    decisions["v01m2_c02"] = "fix: убрать надпись"
    export = {"run_id": rid, "gate": "g2", "reviewer": "tester", "decisions": decisions,
              "notes": {"v01_c01": "фон отличный"}}  # fmt: skip
    path = ws.run_dir(rid) / "g2-page.json"
    path.write_text(json.dumps(export, ensure_ascii=False))
    result = CliRunner().invoke(
        app, ["review", rid, "--gate", "g2", "--from-file", str(path), "--runs-dir",
              str(ws.runs_dir)],
    )  # fmt: skip
    assert result.exit_code in (0, 2), result.output
    events = [e for e in store.events() if e.type == "human_decision"]
    assert len(events) == 20
    first = next(e for e in events if e.card_id == "v01_c01")
    assert first.payload["decision"] == "accept" and first.payload["note"] == "фон отличный"
    assert next(e for e in events if e.card_id == "v01m2_c02").payload["decision"] == "fix"
    wrong = dict(export, run_id="other")
    path.write_text(json.dumps(wrong))
    bad = CliRunner().invoke(
        app, ["review", rid, "--gate", "g2", "--from-file", str(path), "--runs-dir",
              str(ws.runs_dir)],
    )  # fmt: skip
    assert bad.exit_code == 1


def test_fixed_card_returns_to_g2_and_partial_run_reopens(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="again")
    result = service.advance(ws, rid)
    assert result["state"] == "awaiting_g2" and len(result["waiting"]) == 30
    decisions = {cid: "accept" for cid in result["waiting"]}
    decisions["v01_c01"] = "fix: проще"
    service.decide_gate(ws, rid, "g2", decisions, reviewer="tester")
    result = service.advance(ws, rid)
    # The redrawn card comes back as a new G2 item; the old fix stays in the history.
    assert result["state"] == "awaiting_g2" and result["waiting"] == ["v01_c01"]
    gate = RunStore(ws.run_dir(rid)).read_json("review/g2.json")
    assert gate["history"][0]["card_id"] == "v01_c01" and "v01_c01" not in gate["decisions"]
    service.decide_gate(ws, rid, "g2", {"v01_c01": "reject"}, reviewer="tester")
    assert service.advance(ws, rid)["state"] == "partial"
    with pytest.raises(service.ServiceError, match="reason"):
        service.reopen(ws, rid, " ")
    assert service.reopen(ws, rid, "test")["state"] == "rendering"
    assert service.advance(ws, rid)["state"] == "partial"


def test_set_budget_is_journaled(ws):
    rid = new_run(ws, brief(budget=Budget(max_cost_usd=0.1)),
                  fake_backend={"cost_per_image_usd": 0.04})  # fmt: skip
    assert service.advance(ws, rid)["reason"] == "budget_exhausted"
    with pytest.raises(service.ServiceError, match="reason"):
        service.set_budget(ws, rid, 2.0, "")
    service.set_budget(ws, rid, 2.0, "more budget", max_attempts_per_card=5)
    store = RunStore(ws.run_dir(rid))
    budget = store.read_model("request.json", CollectionRequest).budget
    assert budget.max_cost_usd == 2.0 and budget.max_attempts_per_card == 5
    change = [e for e in store.events() if e.payload.get("kind") == "budget_changed"]
    assert change and change[0].payload["from"] == 0.1
    service.reopen(ws, rid, "budget raised")
    assert service.advance(ws, rid)["state"] == "completed"


def test_gpt_judge_drops_zdr_only_when_told():
    """The user allowed the GPT judge without zero data retention; the default keeps ZDR."""
    from cardforge.evaluate.batch_judges import OpenRouterAbsoluteJudge

    def body():
        return {"provider": {"only": ["openai"], "zdr": True, "ignore": ["openai", "azure"]}}

    strict = OpenRouterAbsoluteJudge(ROOT).provider_settings(body())["provider"]
    loose = OpenRouterAbsoluteJudge(ROOT, zdr=False).provider_settings(body())["provider"]
    assert strict["zdr"] is True and "zdr" not in loose
    assert strict["ignore"] == loose["ignore"] == ["azure"]  # Azure stays excluded


def test_primary_render_alias_uses_the_main_generators(ws):
    rid = service.create_run(
        ws, brief(hitl=Hitl(g1="none", g2="all"), budget=Budget(max_cost_usd=0.01)),
        llm="fake", render="primary", judge="none",
        run_id="primary",
    )  # fmt: skip
    m = RunStore(ws.run_dir(rid)).manifest()
    assert m.render_profiles == ["or-gpt-image-2-5"]  # 2026-11-09: Nano is the reserve (G1)
    assert m.render_fallbacks == {"or-gpt-image-2-5": "or-nano-banana-v2"}
    with pytest.raises(service.ServiceError, match="switched off"):
        service.create_run(ws, brief(), llm="fake", render="local-klein4b", judge="fake")


def test_blind_review_package_hides_models_and_imports(tmp_path):
    import importlib.util
    import shutil
    import subprocess

    from PIL import Image as _Image

    spec_ = importlib.util.spec_from_file_location(
        "blind", ROOT / "scripts/stage04_blind_review.py"
    )
    blind = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(blind)
    rows = []
    for n, (obj, model) in enumerate(
        [("Шляпа", "or-gpt-image-2"), ("Шляпа", "or-seedream-5-pro"),
         ("Камера", "or-nano-banana-v2"), ("Камера", "or-gpt-image-2"),
         ("Кресло", "or-flux3-v2"), ("Кресло", "or-gpt-image-2")]
    ):  # fmt: skip
        image = tmp_path / f"src-{n}.png"
        _Image.new("RGB", (430, 480), (n * 30, 80, 120)).save(image)
        rows.append(
            {"item_id": f"run/{n}", "model": model, "image": str(image), "image_sha256": "x",
             "spec": {"object_name": obj, "category": 1, "set_theme": "Кино · Вестерн",
                      "must_not_have": []},
             "open_g2": {"decision": "accept" if n % 2 else "reject"}}
        )  # fmt: skip
    out = tmp_path / "pkg"
    result = blind.package(out, rows)
    assert result["cards"] == 6
    page = (out / blind.NAME / "index.html").read_text()
    assert "or-gpt-image" not in page and "seedream" not in page and "run/" not in page
    script = page.split("<script>")[1].split("</script>")[0]
    (tmp_path / "page.js").write_text(script)
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(tmp_path / "page.js")]).returncode == 0
    mapping = json.loads((out / "mapping.private.json").read_text())["cards"]
    names = [m["spec"]["object_name"] for m in mapping]
    assert all(a != b for a, b in zip(names, names[1:], strict=False))
    with pytest.raises(SystemExit):
        blind.package(out, rows)  # never reshuffled
    for who, decisions in (("А", "accept"), ("Б", "reject")):
        export = {
            "format": blind.FORMAT,
            "reviewer": who,
            "ratings": {
                m["card"]: {"image_sha256": m["package_sha256"], "decision": decisions,
                            "reasons": [] if decisions == "accept" else ["crop", "other"],
                            "note": ""}
                for m in mapping
            },
        }  # fmt: skip
        path = tmp_path / f"{who}.json"
        path.write_text(json.dumps(export, ensure_ascii=False))
        report = blind.import_export(path, out)
    assert report["agreement"]["pairs"]["А vs Б"]["strict"]["agreement"] == 0.0
    assert report["agreement"]["vs_open_g2"]["А"]["strict"]["n"] == 6
    bad = json.loads(path.read_text())
    bad["reviewer"] = "В"
    bad["ratings"][mapping[0]["card"]]["reasons"] = ["nonsense"]
    path.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="unknown reason"):
        blind.import_export(path, out)
