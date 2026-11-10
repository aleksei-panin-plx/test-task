"""Decisions of 2026-11-09 (docs/research/autonomy-readiness-2026-11-08.md §8, user «Все пять как
рекомендовано»): G1 Nano reserve, J4 Kimi only on the style grey zone, §4.4 shadow auto-pass
journal, G7 hard-check redraw accounting. Offline (FakeBackend / FakeLLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.evaluate import auto_reject, card_qa, style_scorer
from cardforge.orchestrator import service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


@pytest.fixture(autouse=True)
def active_step0(monkeypatch):
    monkeypatch.setattr(auto_reject, "stopped", lambda root, cfg: False)


def brief(g2="none") -> CollectionRequest:
    return CollectionRequest(
        request_id="t", collection_theme="Кино", set_theme="Вестерн",
        producer_wishes_raw="побольше ретро", render_profile="fake",
        hitl=Hitl(g1="none", g2=g2),
    )  # fmt: skip


def patch_config(monkeypatch, fn) -> None:
    original = Workspace.config

    def patched(self):
        cfg = original(self)
        fn(cfg)
        return cfg

    monkeypatch.setattr(Workspace, "config", patched)


def tiny_model(tau_a=None, tau_r=0.7, dim=8) -> dict:
    return {
        "version": style_scorer.VERSION, "name": "tiny", "embedding": {"dim": dim},
        "probe": {"coef": [3.0] + [0.0] * (dim - 1), "intercept": 0.0,
                  "mean": [0.0] * dim, "std": [1.0] * dim},
        "zones": {"tau_a": tau_a, "tau_r": tau_r},
    }  # fmt: skip


def events(ws, rid, kind):
    return [e for e in RunStore(ws.run_dir(rid)).events() if e.type == kind]


# ---------------------------------------------------------------- G1


def test_project_primary_is_gpt_only_with_nano_reserve(ws):
    cfg = ws.config()
    assert cfg["defaults"]["primary_render_profiles"] == ["or-gpt-image-2-5"]
    assert cfg["defaults"]["fallback_after_failures"] == 2
    assert cfg["render_profiles"]["or-gpt-image-2-5"]["fallback"] == "or-nano-banana-v2"
    rid = service.create_run(
        ws, brief("all").model_copy(update={"budget": Budget(max_cost_usd=0.01)}),
        llm="fake", render="primary", judge="none", run_id="primary",
    )  # fmt: skip
    m = RunStore(ws.run_dir(rid)).manifest()
    assert m.render_profiles == ["or-gpt-image-2-5"]
    assert m.render_fallbacks == {"or-gpt-image-2-5": "or-nano-banana-v2"}
    off = service.create_run(
        ws, brief("all").model_copy(update={"budget": Budget(max_cost_usd=0.01)}),
        llm="fake", render="primary", judge="none", run_id="off",
        runtime_options={"render_fallback": False},
    )  # fmt: skip
    assert RunStore(ws.run_dir(off)).manifest().render_fallbacks == {}


def _fallback_run(ws, monkeypatch, run_id, **options):
    patch_config(monkeypatch, lambda c: c["render_profiles"]["fake"].update(fallback="fake-b"))
    script = {"fake_judge": {"judge_script": {"v01_c02": ["fix", "fix", "accept"],
                                              "v01_c03": ["fix", "accept"]}}}  # fmt: skip
    return service.create_run(
        ws, brief(), llm="fake", render="fake", judge="fake", shadow_judges=[], run_id=run_id,
        runtime_options={**script, **options},
    )  # fmt: skip


def test_card_moves_to_the_reserve_after_two_failures_in_a_row(ws, monkeypatch):
    rid = _fallback_run(ws, monkeypatch, "fb")
    assert service.advance(ws, rid)["state"] == "completed"
    store = RunStore(ws.run_dir(rid))
    m = store.manifest()
    assert m.render_fallbacks == {"fake": "fake-b"}
    moved = m.counters["fallback"]
    assert set(moved) == {"v01_c02"}  # v01_c03 failed once only
    assert moved["v01_c02"]["profile"] == "fake-b" and moved["v01_c02"]["at_attempt"] == 3
    assert [a["decision"] for a in moved["v01_c02"]["after"]] == ["fix", "fix"]

    def model(n):
        return store.read_json(f"cards/v01/v01_c02/attempt_{n:02d}/attempt.json")["model_id"]

    v1, v1b = "fake-backend-v1", "fake-backend-v1b"
    assert [model(n) for n in (1, 2, 3)] == [v1, v1, v1b]
    assert [e.card_id for e in events(ws, rid, "render_fallback")] == ["v01_c02"]
    assert "Резервный генератор" in store.path("report.md").read_text()


def test_reserve_can_be_switched_off_per_run(ws, monkeypatch):
    rid = _fallback_run(ws, monkeypatch, "nofb", render_fallback=False)
    assert service.advance(ws, rid)["state"] == "completed"
    store = RunStore(ws.run_dir(rid))
    assert store.manifest().render_fallbacks == {}
    third = store.read_json("cards/v01/v01_c02/attempt_03/attempt.json")
    assert third["model_id"] == "fake-backend-v1"


def test_test_profile_cannot_fall_back_to_a_real_generator(ws, monkeypatch):
    patch_config(monkeypatch, lambda c: c["render_profiles"]["fake"].update(
        fallback="or-nano-banana-v2"))  # fmt: skip
    with pytest.raises(service.ServiceError, match="test render profile"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake", run_id="x")


# ---------------------------------------------------------------- J4


def test_style_gate_skip_rule():
    gate = {"min_p": 0.3, "runs": "shadow"}
    sig = lambda p: {"value": p, "tau_r": 0.625}  # noqa: E731
    assert service.style_gate_skip(gate, sig(0.1), True).startswith("skipped: style_gate")
    assert service.style_gate_skip(gate, sig(0.3), True) is None
    assert service.style_gate_skip(gate, sig(0.6), True) is None
    assert "tau_r" in service.style_gate_skip(gate, sig(0.7), True)
    assert service.style_gate_skip(gate, sig(0.1), False) is None  # not a shadow run
    assert service.style_gate_skip({**gate, "runs": "all"}, sig(0.1), False) is not None
    assert service.style_gate_skip(gate, {"value": "not_run"}, True) is None  # no score: ask
    assert service.style_gate_skip({}, sig(0.1), True) is None


def test_project_kimi_veto_is_gated_on_the_grey_zone(ws):
    from cardforge.orchestrator.workspace import judge_profile

    assert judge_profile(ws, "kimi-veto-v2").style_gate == {"min_p": 0.3, "runs": "shadow"}


def test_shadow_run_asks_the_veto_judge_only_in_the_grey_zone(ws, monkeypatch):
    patch_config(monkeypatch, lambda c: c["judges"]["fake-veto-v2"].update(
        style_gate={"min_p": 0.3, "runs": "shadow"}))  # fmt: skip
    rid = service.create_run(
        ws, brief("all"), llm="fake", render="fake", shadow_judges=["fake-veto-v2"], run_id="j4",
        runtime_options={"shadow": True, "fake_style": {"model": tiny_model(), "mode": "shadow"}},
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    asked = skipped = 0
    for path in ws.run_dir(rid).glob("cards/*/*/attempt_*/eval.json"):
        ev = json.loads(path.read_text())
        p = ev["hard_checks"]["style_score"]["value"]
        answered = (path.parent / "judges/fake-veto-v2.json").exists()
        if ev["hard_checks"].get("crop", {}).get("passed") is False:
            continue
        assert answered == (0.3 <= p < 0.7), (path, p)
        if not answered:
            assert ev["shadow_judges"]["fake-veto-v2"].startswith("skipped: style_gate")
        asked += answered
        skipped += not answered
    assert asked and skipped
    gates = [e for e in RunStore(ws.run_dir(rid)).events()
             if e.payload.get("check") == "style_gate:fake-veto-v2"]  # fmt: skip
    assert len(gates) == skipped


# ---------------------------------------------------------------- §4.4 shadow auto-pass


def test_shadow_auto_pass_is_journaled_and_never_acts(ws, monkeypatch):
    monkeypatch.setattr(service, "shadow_auto_pass_config",
                        lambda root: {"fake": {"tau_a": 0.5, "stub": True}})  # fmt: skip
    style = {"model": tiny_model(), "mode": "shadow"}
    plain = service.create_run(
        ws, brief("all"), llm="fake", render="fake", shadow_judges=[], run_id="plain",
        runtime_options={"shadow": True, "fake_style": style},
    )  # fmt: skip
    assert service.advance(ws, plain)["state"] == "awaiting_g2"
    passes = events(ws, plain, "auto_pass_shadow")
    assert passes and all(not e.payload["acts"] and e.payload["stub"] for e in passes)
    assert all(e.payload["p"] <= 0.5 and e.payload["core_ok"] for e in passes)
    for path in ws.run_dir(plain).glob("cards/*/*/attempt_*/eval.json"):
        ev = json.loads(path.read_text())
        rec = ev["hard_checks"]["style_score"]["shadow_auto_pass"]
        assert rec["in_zone"] == (rec["p"] <= 0.5 and rec["core_ok"])
        assert ev["decision"] != "accept" or ev["decided_by"] == "auto"
    m = RunStore(ws.run_dir(plain)).manifest()
    assert all(c.state in ("accepted", "flagged") for c in m.cards.values())  # G2 decides


def test_project_shadow_auto_pass_keeps_the_pinned_model():
    entry = service.shadow_auto_pass_config(ROOT)["or-gpt-image-2-5"]
    assert entry["model_sha256"] == style_scorer.load_config(ROOT)["model_sha256"]


# ---------------------------------------------------------------- G7 hard-check redraws


def legacy_geometry(monkeypatch):
    """crop / safe_zone as hard fails (before 2026-11-09) for tests of the redraw machinery."""
    monkeypatch.setattr(card_qa, "flag_only_checks", lambda mask_config=None: frozenset())


def test_hard_check_redraw_outcome_is_counted(ws, monkeypatch):
    legacy_geometry(monkeypatch)
    rid = service.create_run(
        ws, brief(), llm="fake", render="fake", judge="fake", shadow_judges=[], run_id="g7",
        runtime_options={"fake_mask": {"crop": {"v01_c02": [1]}}},
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "completed"
    [e] = events(ws, rid, "hard_check_redraw")
    assert e.card_id == "v01_c02" and e.payload["checks"] == ["crop"]
    assert e.payload["from_attempt"] == 1 and e.payload["decision"] == "accept"
    assert e.payload["still_failing"] == [] and e.payload["after"].endswith("attempt_02/final.png")
    summary = json.loads((ws.run_dir(rid) / "hard_check_redraws.json").read_text())
    assert summary["per_check"]["crop"]["redraws"] == 1
    assert summary["per_check"]["crop"]["card_state"] == {"accepted": 1}
    assert summary["rows"][0]["redraw_accepted"] is True


def test_geometry_hard_checks_do_not_move_a_card_to_the_reserve(ws, monkeypatch):
    """User 2026-11-09 «Да, так»: only a person, a judge or the scorer count as a GPT failure."""
    legacy_geometry(monkeypatch)
    patch_config(monkeypatch, lambda c: c["render_profiles"]["fake"].update(fallback="fake-b"))
    rid = service.create_run(
        ws, brief(), llm="fake", render="fake", judge="fake", shadow_judges=[], run_id="geo",
        runtime_options={"fake_mask": {"crop": {"v01_c02": [1, 2]}}},
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "completed"
    store = RunStore(ws.run_dir(rid))
    m = store.manifest()
    assert m.counters["failures"]["v01_c02"] == {"1": ["hard_check:crop"], "2": ["hard_check:crop"]}
    assert "fallback" not in m.counters and not events(ws, rid, "render_fallback")
    third = store.read_json("cards/v01/v01_c02/attempt_03/attempt.json")
    assert third["model_id"] == "fake-backend-v1"


def test_fallback_failure_sources():
    assert service.fallback_failure(["judge"]) is True
    assert service.fallback_failure(["hard_check:crop", "human"]) is True
    assert service.fallback_failure(["auto_reject"]) is True
    assert service.fallback_failure(["hard_check:crop", "hard_check:text"]) is None
    assert service.fallback_failure(["policy:IP-QA-01", "fix_check"]) is None
    assert service.fallback_failure(None) is None


def test_kill_switch_stops_queued_renders_before_the_paid_call(ws, monkeypatch):
    """shadow-02: a batch of queued renders ran past runs/STOP. Now every job checks it first."""
    from cardforge.orchestrator import execution

    rid = service.create_run(
        ws, brief().model_copy(update={"budget": Budget(max_cost_usd=10.0)}), llm="fake",
        render="fake", judge="fake", shadow_judges=[], run_id="queued",
        runtime_options={"fake_backend": {"cost_per_image_usd": 0.05}},
    )  # fmt: skip
    import sys

    real = execution.stop_requested

    def stop_in_jobs(runs_dir):  # STOP appears after the batch was queued
        return sys._getframe(1).f_code.co_name == "_render_job"

    monkeypatch.setattr(service, "stop_requested", stop_in_jobs)
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    assert not list(store.root.glob("cards/*/*/attempt_*/attempt.json"))  # no call made
    costs = store.read_json("costs.json")["calls"]
    renders = [c for c in costs if c["kind"] == "render"]
    assert renders and all(c["actual_usd"] == 0.0 for c in renders)  # reservations released
    assert any(e.payload.get("kind") == "stopped" for e in store.events() if e.type == "error")
    monkeypatch.setattr(service, "stop_requested", real)
    assert service.advance(ws, rid)["state"] == "completed"  # resume renders the attempts
    assert len(list(store.root.glob("cards/*/*/attempt_01/attempt.json"))) == 30


# ---------------------------------------------------------------- crop / safe_zone → flags


def test_project_config_makes_crop_and_safe_zone_flags_only():
    assert card_qa.flag_only_checks() == {"crop", "safe_zone"}


def test_crop_is_a_flag_for_the_person_no_redraw_and_journaled(ws, monkeypatch):
    """User 2026-11-09 «Да, по плану»: crop / safe_zone go to G2 as a flag, no redraw, no
    auto-reject; eval.json keeps `would: fix`; the card is not an auto-pass candidate."""
    monkeypatch.setattr(service, "shadow_auto_pass_config",
                        lambda root: {"fake": {"tau_a": 1.0, "stub": True}})  # fmt: skip
    rid = service.create_run(
        ws, brief("all"), llm="fake", render="fake", shadow_judges=[], run_id="flags",
        runtime_options={"shadow": True, "fake_mask": {"crop": {"v01_c02": [1]}},
                         "fake_style": {"model": tiny_model(), "mode": "shadow"}},
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    card = store.manifest().cards["v01_c02"]
    assert card.attempts == 1 and card.state == "flagged"  # no redraw
    ev = store.read_json("cards/v01/v01_c02/attempt_01/eval.json")
    assert "hard_check:crop:flag" in ev["reasons"] and ev["fixes"] == []
    crop = ev["hard_checks"]["crop"]
    assert crop["would"] == "fix" and crop["measured_passed"] is False and crop["passed"] is None
    assert ev["hard_checks"]["style_score"]["shadow_auto_pass"]["core_ok"] is False
    gate = store.read_json("review/g2.json")
    item = next(i for i in gate["items"] if i["card_id"] == "v01_c02")
    assert "hard_check:crop:flag" in item["reasons"]  # visible to the person on G2
    assert not events(ws, rid, "hard_check_redraw")


# ---------------------------------------------------------------- real auto-pass (2026-11-09)


def real_auto_pass(monkeypatch, tmp_path, **cfg):
    """Auto-pass on for the fake profile, its state file in tmp."""
    conf = {"enabled": True, "profiles": ["fake"], "window": 100, "max_return_share": 0.2,
            "state": str(tmp_path / "autopass.json"), **cfg}  # fmt: skip
    monkeypatch.setattr(service, "auto_pass_config", lambda root: dict(conf))
    monkeypatch.setattr(service, "shadow_auto_pass_config",
                        lambda root: {"fake": {"tau_a": 0.5, "stub": False}})  # fmt: skip


def ap_run(ws, rid, **opts):
    return service.create_run(
        ws, brief("all"), llm="fake", render="fake", shadow_judges=[], run_id=rid,
        runtime_options={"fake_style": {"model": tiny_model(), "mode": "shadow"}, **opts},
    )  # fmt: skip


def test_project_auto_pass_is_on_for_gpt_with_the_approved_threshold():
    cfg = service.auto_pass_config(ROOT)
    assert cfg["enabled"] and cfg["profiles"] == ["or-gpt-image-2-5"]
    assert cfg["window"] == 100 and cfg["max_return_share"] == 0.2
    entry = service.shadow_auto_pass_config(ROOT)["or-gpt-image-2-5"]
    assert entry["tau_a"] == 0.564 and not entry["stub"] and entry["basis"] == "approved-2026-11-09"


def test_auto_passed_cards_skip_g2_and_are_journaled(ws, monkeypatch, tmp_path):
    real_auto_pass(monkeypatch, tmp_path)
    rid = ap_run(ws, "ap")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    auto = store.manifest().counters["auto_pass"]
    assert auto and all(store.manifest().cards[c].state == "accepted" for c in auto)
    gate = store.read_json("review/g2.json")
    assert not {i["card_id"] for i in gate["items"]} & set(auto)  # no G2 for them
    for cid in auto:
        ev = (
            store.read_json(f"cards/v01/{cid}/attempt_01/eval.json")
            if cid.startswith("v01")
            else None
        )
        if ev:
            assert ev["reasons"] == ["auto_pass"] and ev["decision"] == "accept"
            assert ev["hard_checks"]["style_score"]["shadow_auto_pass"]["acts"] is True
    assert {e.card_id for e in events(ws, rid, "auto_pass")} == set(auto)
    state = json.loads((tmp_path / "autopass.json").read_text())
    assert {r["card"] for r in state["passed"]} == set(auto)
    # every other card is still the person's
    assert {i["card_id"] for i in gate["items"]} | set(auto) == set(store.manifest().cards)


def test_shadow_run_and_switches_keep_auto_pass_a_journal(ws, monkeypatch, tmp_path):
    real_auto_pass(monkeypatch, tmp_path)
    rid = ap_run(ws, "apshadow", shadow=True)
    service.advance(ws, rid)
    assert not events(ws, rid, "auto_pass") and events(ws, rid, "auto_pass_shadow")
    assert "auto_pass" not in RunStore(ws.run_dir(rid)).manifest().counters
    assert service.auto_pass_status(ws, {"shadow": True}) == (False, "shadow run (--shadow)")
    (ws.runs_dir / "STOP").touch()
    assert service.auto_pass_status(ws, {})[1] == "kill switch runs/STOP"
    (ws.runs_dir / "STOP").unlink()
    real_auto_pass(monkeypatch, tmp_path, enabled=False)
    assert service.auto_pass_status(ws, {})[0] is False


def test_g3_return_sends_the_card_to_g2_and_trips_the_breaker(ws, monkeypatch, tmp_path):
    real_auto_pass(monkeypatch, tmp_path, window=4)  # > 20 % of 4 = one return trips it
    rid = ap_run(ws, "g3")
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    gate = store.read_json("review/g2.json")
    service.decide_gate(
        ws, rid, "g2", {i["card_id"]: "accept" for i in gate["items"]}, reviewer="R1"
    )
    assert service.advance(ws, rid)["state"] == "completed"
    g3 = store.read_json("review/g3.json")
    marked = [c["card_id"] for v in g3["variants"].values() for c in v["auto_passed"]]
    assert marked and g3["auto_pass"]["acts"] is True
    # the breaker looks at the last `window` auto-passed cards: return the most recent one
    cid = json.loads((tmp_path / "autopass.json").read_text())["passed"][-1]["card"]
    assert cid in marked
    with pytest.raises(service.ServiceError, match="reason"):
        service.g3_return(ws, rid, cid, " ", "R1")
    service.g3_return(ws, rid, cid, "материал не в стиле", "R1")
    m = store.manifest()
    assert (
        m.cards[cid].state == "flagged"
        and m.counters["auto_pass"][cid]["returned"]["reviewer"] == "R1"
    )
    assert [e.card_id for e in events(ws, rid, "auto_pass_returned")] == [cid]
    assert events(ws, rid, "auto_pass_disabled")  # 1 of 4 > 20 %
    assert service.auto_pass_status(ws, {})[1].startswith("breaker:")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    gate = store.read_json("review/g2.json")
    assert cid in {i["card_id"] for i in gate["items"]}  # the person decides it now
    with pytest.raises(service.ServiceError, match="already returned"):
        service.g3_return(ws, rid, cid, "again", "R1")
