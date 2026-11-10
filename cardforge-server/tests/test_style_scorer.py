"""Layer-1 style scorer, risk zones, step-0 auto-reject and candidate ranking
(docs/research/fable-review.md §3.2–3.4, Q2). Offline: no model weights, no labels."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from cardforge.contracts.generation import JudgeOutcome
from cardforge.contracts.request import CollectionRequest, Hitl
from cardforge.evaluate import auto_reject, card_qa, style_scorer, veto_judge
from cardforge.orchestrator import service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


def run(ws, run_id, shadows, **options) -> str:
    request = CollectionRequest(
        request_id="t", collection_theme="Кино", set_theme="Вестерн",
        producer_wishes_raw="побольше ретро", render_profile="fake",
        hitl=Hitl(g1="none", g2="all"),
    )  # fmt: skip
    return service.create_run(
        ws, request, llm="fake", render="fake", shadow_judges=shadows, run_id=run_id,
        runtime_options=options,
    )  # fmt: skip


def card_states(ws, rid) -> dict:
    return {c: (m.state, m.attempts) for c, m in RunStore(ws.run_dir(rid)).manifest().cards.items()}


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


@pytest.fixture(autouse=True)
def active_step0(monkeypatch):
    """Tests must not depend on the monitor's state file of the working copy."""
    monkeypatch.setattr(auto_reject, "stopped", lambda root, cfg: False)


# ---------------------------------------------------------------- statistics


def test_logreg_separates_and_matches_closed_form_direction():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(200, 3))
    y = (X[:, 0] - 0.5 * X[:, 1] + 0.3 * rng.normal(size=200) > 0).astype(int)
    m = style_scorer.fit_logreg(X, y, C=1.0, standardize=False)
    assert m["coef"][0] > 0 > m["coef"][1]
    p = style_scorer.predict(m, X)
    assert style_scorer.auc(p, y) > 0.95
    # balanced weights move the 0.5 point to the balanced decision point
    yb = (X[:, 0] > 1.0).astype(int)
    w = np.where(yb == 1, 0.5 / yb.mean(), 0.5 / (1 - yb.mean()))
    plain = style_scorer.predict(style_scorer.fit_logreg(X, yb, C=0.01), X)
    bal = style_scorer.predict(style_scorer.fit_logreg(X, yb, C=0.01, sample_weight=w), X)
    assert (bal >= 0.5).mean() > (plain >= 0.5).mean()


def test_auc_kappa_and_clopper_pearson_known_values():
    assert style_scorer.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert style_scorer.auc([0.5, 0.5], [0, 1]) == 0.5
    assert style_scorer.kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0
    lo, hi = style_scorer.clopper_pearson(0, 10)
    assert lo == 0.0 and abs(hi - 0.3085) < 1e-3
    lo, hi = style_scorer.clopper_pearson(5, 10)
    assert abs(lo - 0.1871) < 1e-3 and abs(hi - 0.8129) < 1e-3


def test_group_folds_keep_a_position_in_one_fold():
    groups = [f"g{i // 2}" for i in range(40)]
    folds = style_scorer.group_folds(groups, 5, seed=3)
    for g in set(groups):
        assert len({f for f, gg in zip(folds, groups, strict=True) if gg == g}) == 1


def test_zones_respect_risk_and_ci_and_can_be_empty():
    rng = np.random.default_rng(1)
    s = rng.uniform(size=2000)
    y = (rng.uniform(size=2000) < s).astype(int)  # P(reject) = score
    z = style_scorer.calibrate_zones(s, y, r_a=0.2, r_r=0.1, slack=0.05, min_n=20)
    assert z["tau_a"] is not None and z["tau_r"] is not None and z["tau_a"] < z["tau_r"]
    assert z["auto_pass"]["rate"] <= 0.2 and z["auto_pass"]["ci95"][1] <= 0.25
    assert z["auto_reject"]["rate"] <= 0.1 and z["auto_reject"]["ci95"][1] <= 0.15
    rep = style_scorer.zone_report(s, y, z, ["a"] * 1000 + ["b"] * 1000)
    assert (
        rep["all"]["auto_pass"]["n"] + rep["all"]["auto_reject"]["n"] + rep["all"]["grey"] == 2000
    )
    assert set(rep) == {"all", "a", "b"}
    # a useless score on few cards: no zone is feasible
    z0 = style_scorer.calibrate_zones(np.linspace(0, 1, 30), np.tile([0, 1], 15))
    assert z0["tau_a"] is None and z0["tau_r"] is None
    assert style_scorer.zone_of(0.5, z0) == "grey"


# ---------------------------------------------------------------- artifact, modes, signal


def tiny_model(tau_a=0.2, tau_r=0.8, dim=8) -> dict:
    return {
        "version": style_scorer.VERSION,
        "name": "tiny",
        "embedding": {"dim": dim},
        "probe": {
            "coef": [3.0] + [0.0] * (dim - 1),
            "intercept": 0.0,
            "mean": [0.0] * dim,
            "std": [1.0] * dim,
        },  # fmt: skip
        "zones": {"tau_a": tau_a, "tau_r": tau_r},
    }


def test_mode_refuses_auto_pass_and_unknown():
    assert style_scorer.check_mode({}) == "shadow"
    assert style_scorer.check_mode({"mode": "auto_reject"}) == "auto_reject"
    with pytest.raises(style_scorer.ScorerError, match="auto-pass stays forbidden"):
        style_scorer.check_mode({"mode": "auto_pass"})


def test_artifact_hash_is_verified(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(tiny_model()))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    m = style_scorer.load_model(tmp_path, {"model": "m.json", "model_sha256": digest})
    assert m["_sha256"] == digest
    with pytest.raises(style_scorer.ScorerError, match="changed"):
        style_scorer.load_model(tmp_path, {"model": "m.json", "model_sha256": "0" * 64})


def test_signal_is_signal_only_and_acts_only_in_auto_reject_mode():
    m = tiny_model()
    hi = style_scorer.card_signal(0.95, m, "shadow")
    assert hi["passed"] is None and hi["required"] is False and hi["signal_only"]
    assert hi["zone"] == "auto_reject" and hi["would"] == "reject" and not hi["acts"]
    assert style_scorer.card_signal(0.95, m, "auto_reject")["acts"]
    assert style_scorer.card_signal(0.1, m, "auto_reject")["would"] == "pass"
    assert not style_scorer.card_signal(0.1, m, "auto_reject")["acts"]  # never auto-pass
    assert style_scorer.card_signal(None, None, "shadow", "x")["value"] == "not_run"


def test_project_config_is_shadow_and_pinned():
    cfg = style_scorer.load_config(ROOT)
    assert cfg["mode"] == "shadow" and len(cfg["model_sha256"]) == 64
    assert cfg["zones"]["r_a"] == 0.20


# ---------------------------------------------------------------- ranking (Q2)


def test_rank_candidates_orders_by_score_hard_checks_and_vetoes():
    C = style_scorer.Candidate
    rows = style_scorer.rank_candidates(
        [
            C("hard", 0.05, hard_failed=("crop",)),
            C("precise", 0.10, vetoes=("k:downward",), vetoes_precise=("k:downward",)),
            C("worse", 0.60),
            C("better", 0.30),
            C("soft", 0.20, vetoes=("k:extra_props",)),
            C("unscored", None),
        ]
    )
    assert [r["id"] for r in rows] == ["better", "soft", "unscored", "worse", "precise", "hard"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert not rows[-1]["eligible"] and not rows[-2]["eligible"] and rows[0]["eligible"]
    cand = style_scorer.candidate_from_eval(
        "x", {"crop": {"passed": False}, "look": {"passed": None}, "text": {"passed": None,
              "required": True}}, 0.4, [], [])  # fmt: skip
    assert cand.hard_failed == ("crop",) and cand.hard_unknown == ("text",)


# ---------------------------------------------------------------- step 0


def outcome(*items) -> JudgeOutcome:
    return JudgeOutcome(
        judge_model="x",
        decision="reject" if items else "accept",
        decision_policy="veto_only",
        criteria={k: {"defect": k in items, "note": "n"} for k in veto_judge.V2_DEFECTS},
    )


def test_v2_checklist_is_the_frozen_phase4_prompt():
    assert hashlib.sha256(veto_judge.V2_PROMPT.encode()).hexdigest() == veto_judge.V2_PROMPT_SHA256
    assert veto_judge.checklist("defect-checklist-v2") is veto_judge.V2
    assert set(veto_judge.V2_CODES.values()) <= set(veto_judge.DEFECT_CODES.values())


def test_v2_outcome_records_every_found_item():
    data = {"extra_items": []} | {
        k: {"defect": k in ("downward", "fragmented"), "note": "n"} for k in veto_judge.V2_DEFECTS
    }
    out, rec = veto_judge.veto_outcome(data, "p", "m")
    assert rec["profile"] == "defect-checklist-v2" and rec["found"] == ["fragmented", "downward"]
    assert rec["defects"] == ["fragmented"] and out.decision == "reject"  # a-priori v2 veto set
    assert out.criteria["downward"]["defect"] is True


def test_step0_admits_only_measured_profile_items():
    cfg = auto_reject.load_config(ROOT)
    assert cfg["veto_rules"][0]["judge_profile"] == "defect-checklist-v2"
    assert set(cfg["veto_rules"][0]["items"]) == {"style_mismatch", "downward"}
    assert "text" not in cfg.get("hard_checks", {})  # 73 % on blind labels: not admitted
    shadows = {
        "v2": outcome("style_mismatch", "second_ground_line"),
        "v1": outcome("style_mismatch"),
        "gone": "error: timeout",
    }
    profiles = {"v2": "defect-checklist-v2", "v1": "defect-checklist-v1"}
    fired = auto_reject.veto_firings(shadows, profiles, cfg)
    by = {f["source"]: f["admitted"] for f in fired}
    assert by == {"v2:style_mismatch": True, "v2:second_ground_line": False,
                  "v1:style_mismatch": False}  # fmt: skip
    acted = auto_reject.acting(fired, True)
    assert [f["source"] for f in acted] == ["v2:style_mismatch"]
    assert auto_reject.acting(fired, False) == []
    payload = auto_reject.event_payload(
        fired, acted, attempt_no=1, image="i", image_sha256="s", cfg=cfg, is_active=True
    )
    assert payload["check"] == "auto_reject" and payload["codes"] == ["style"]
    assert payload["acted"] and set(payload["shadow"]) == {"v2:second_ground_line",
                                                          "v1:style_mismatch"}  # fmt: skip


def test_stopped_state_disables_acting(tmp_path, monkeypatch):
    monkeypatch.undo()  # the real `stopped`
    cfg = {"enabled": True, "state": "state.json"}
    assert auto_reject.active(tmp_path, cfg)
    (tmp_path / "state.json").write_text('{"stopped": true}')
    assert not auto_reject.active(tmp_path, cfg)
    (tmp_path / "state.json").write_text("{broken")
    assert not auto_reject.active(tmp_path, cfg)
    assert not auto_reject.active(tmp_path, {"enabled": False})


def test_decide_turns_an_acting_veto_into_fix_with_codes():
    hard = card_qa.HardResult({"format": {"passed": True}})
    profile = card_qa.JudgeProfile(id="none", provider="none")
    ev = card_qa.decide(hard, [], None, profile, {}, auto_rejects=[
        {"source": "kimi-veto-v2:downward", "code": "direction"}])  # fmt: skip
    assert ev.decision == "fix" and "auto_reject:kimi-veto-v2:downward" in ev.reasons
    assert {"source": "auto_reject", "codes": ["direction"]} in ev.fixes
    assert card_qa.decide(hard, [], None, profile, {}).decision == "flag"


# ---------------------------------------------------------------- core run (fake)


def _events(ws, rid, check):
    lines = (ws.run_dir(rid) / "events.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["payload"].get("check") == check]


def test_core_logs_style_score_in_shadow_without_changing_decisions(ws):
    plain = run(ws, "plain", [])
    scored = run(ws, "scored", [], fake_style={"model": tiny_model(), "mode": "shadow"})
    for rid in (plain, scored):
        assert service.advance(ws, rid)["state"] == "awaiting_g2"
    assert card_states(ws, plain) == card_states(ws, scored)
    evals = sorted(ws.run_dir(scored).glob("cards/*/*/attempt_*/eval.json"))
    zones = set()
    for path in evals:
        sig = json.loads(path.read_text())["hard_checks"]["style_score"]
        assert sig["signal_only"] and sig["passed"] is None and not sig["acts"]
        assert 0 <= sig["value"] <= 1
        zones.add(sig["zone"])
    assert zones <= {"auto_reject", "auto_pass", "grey"} and len(zones) >= 2
    assert not _events(ws, scored, "auto_reject")  # shadow: nothing acts, no veto judge


def test_core_step0_auto_rejects_admitted_v2_items_only(ws):
    v1 = run(ws, "v1", ["fake-veto"])
    v2 = run(ws, "v2", ["fake-veto-v2"])
    for rid in (v1, v2):
        service.advance(ws, rid)
    acted_v1 = [e for e in _events(ws, v1, "auto_reject") if e["payload"]["acted"]]
    assert acted_v1 == []  # v1 profile: journaled as shadow, never acts
    v2_events = _events(ws, v2, "auto_reject")
    acted = [e for e in v2_events if e["payload"]["acted"]]
    assert acted, "the fake veto should hit style_mismatch at least once"
    for e in acted:
        assert e["payload"]["vetoes"] == ["fake-veto-v2:style_mismatch"]
        assert e["payload"]["codes"] == ["style"] and len(e["payload"]["image_sha256"]) == 64
        a = e["payload"]["attempt_no"]
        path = next(ws.run_dir(v2).glob(f"cards/*/{e['card_id']}/attempt_{a:02d}/eval.json"))
        ev = json.loads(path.read_text())
        assert ev["decision"] == "fix"
        assert "auto_reject:fake-veto-v2:style_mismatch" in ev["reasons"]
    store = RunStore(ws.run_dir(v2))
    assert store.manifest().cards  # the run stays consistent


def test_pair_term_pulls_the_preferred_side_down():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(60, 4))
    y = (X[:, 0] > 0).astype(int)
    # comparisons say: lower x[1] is closer to the style (left wins when left x1 < right x1)
    L, R = rng.normal(size=(200, 4)), rng.normal(size=(200, 4))
    t = (L[:, 1] < R[:, 1]).astype(float)
    plain = style_scorer.fit_logreg(X, y, C=1.0, standardize=False)
    paired = style_scorer.fit_logreg(X, y, C=1.0, standardize=False,
                                     pairs=(R - L, t, np.ones(200)))  # fmt: skip
    assert paired["coef"][1] > plain["coef"][1] + 0.5  # higher x1 → higher p_reject
    assert paired["coef"][0] > 0
    same = style_scorer.fit_logreg(X, y, C=1.0, standardize=False, pairs=(np.zeros((0, 4)), [], []))
    assert np.allclose(same["coef"], plain["coef"])


# ---------------------------------------------------------------- per-generator zones, shadow


def test_zone_override_per_profile_and_auto_pass_override_refused():
    m = tiny_model(tau_a=None, tau_r=0.44)
    over = style_scorer.check_overrides({"zones_by_profile": {"gpt": {"tau_r": 0.625}}})
    nano = style_scorer.card_signal(0.5, m, "shadow", profile="nano", overrides=over)
    gpt = style_scorer.card_signal(0.5, m, "shadow", profile="gpt", overrides=over)
    assert nano["zone"] == "auto_reject" and nano["zones_source"] == "artifact"
    assert gpt["zone"] == "grey" and gpt["tau_r"] == 0.625
    assert style_scorer.card_signal(0.7, m, "shadow", profile="gpt", overrides=over)["zone"] == (
        "auto_reject"
    )
    with pytest.raises(style_scorer.ScorerError, match="auto-pass"):
        style_scorer.check_overrides({"zones_by_profile": {"gpt": {"tau_a": 0.1}}})
    with pytest.raises(style_scorer.ScorerError, match="unknown"):
        style_scorer.check_overrides({"zones_by_profile": {"gpt": {"tau": 0.1}}})


def test_project_config_pins_rubric04_with_gpt_threshold():
    cfg = style_scorer.load_config(ROOT)
    assert cfg["model"].endswith("model-rubric04-clean.json")
    assert style_scorer.check_overrides(cfg) == {"or-gpt-image-2-5": {"tau_r": 0.625}}
    model = style_scorer.load_model(ROOT, cfg)  # sha256 verified
    assert model["zones"]["tau_a"] is None and abs(model["zones"]["tau_r"] - 0.4426) < 1e-3


def test_force_shadow_never_acts(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(tiny_model()))
    cfg = {"enabled": True, "mode": "auto_reject", "model": "m.json"}
    sc = style_scorer.runtime_scorer(tmp_path, lambda ps: [], cfg, force_shadow=True)
    assert sc.mode == "shadow"
    assert not style_scorer.card_signal(0.95, sc.model, sc.mode)["acts"]


def test_core_shadow_run_journals_step0_and_zones_but_never_acts(ws):
    style = {"model": tiny_model(tau_a=None, tau_r=0.5), "mode": "auto_reject"}
    rid = run(ws, "shadow", ["fake-veto-v2"], shadow=True, fake_style=style)
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    events = _events(ws, rid, "auto_reject")
    assert events and all(not e["payload"]["acted"] for e in events)
    assert all(e["payload"]["shadow_run"] for e in events)
    would = {s for e in events for s in e["payload"]["would_veto"]}
    assert "fake-veto-v2:style_mismatch" in would and "style_score" in would
    for path in ws.run_dir(rid).glob("cards/*/*/attempt_*/eval.json"):
        ev = json.loads(path.read_text())
        assert not any(r.startswith("auto_reject:") for r in ev["reasons"])
        assert not ev["hard_checks"]["style_score"]["acts"]


def test_planner_events_are_valid_event_types():
    """planner.planner emits `material_rules` / `material_budget` (H4, Q1); the event contract
    must accept them (shadow-01: the first real plan with a rewrite failed validation)."""
    import re
    import typing

    from cardforge.contracts.run import EventType

    src = (ROOT / "src/cardforge/planner/planner.py").read_text()
    emitted = set(re.findall(r'store\.emit\(\s*"([a-z_]+)"', src))
    assert {"material_rules", "material_budget"} <= emitted
    assert emitted <= set(typing.get_args(EventType))


def _tiny_index(dim=8):
    R = np.eye(dim)[:4]
    return {"ids": ["a", "b", "c", "d"], "R": R, "pos": {"a": 0, "b": 1, "c": 2, "d": 3},
            "style": [0], "sha256": "x" * 64, "path": "idx.json"}  # fmt: skip


def test_ref_similarity_and_signal_are_signal_only():
    idx = _tiny_index()
    v = np.zeros(8)
    v[1] = 1.0
    sim = style_scorer.ref_similarity(v, idx, ["b", "zz"], k=2)
    assert sim["nearest"] == "b" and sim["bundle_max"] == 1.0 and sim["style_mean"] == 0.0
    assert sim["all_knn5"] == 0.5  # k=2: mean of (1, 0)
    sig = style_scorer.ref_signal(sim, idx)
    assert sig["passed"] is None and sig["required"] is False and sig["signal_only"]
    assert sig["value"] == 0.5 and sig["cosine"]["bundle_mean"] == 1.0
    assert style_scorer.ref_signal(None, None)["value"] == "not_run"
    assert style_scorer.ref_similarity(v, idx, [])["bundle_mean"] is None


def test_scorer_ref_signal_reuses_the_cached_vector(tmp_path):
    img = tmp_path / "c.png"
    img.write_bytes(b"x")
    vec = [0.0, 0.0, 1.0] + [0.0] * 5
    calls = []

    def embed(paths):
        calls.append(paths)
        return [vec]

    sc = style_scorer.Scorer(tiny_model(), "shadow", embed, ref_index=_tiny_index())
    sc.score(img, "s1")
    sig = sc.ref_signal(img, "s1", ["c"])
    assert len(calls) == 1 and sig["nearest"] == "c"
    assert style_scorer.Scorer(tiny_model(), "shadow", embed).ref_signal(img, "s1") is None


def test_project_ref_index_loads_when_present():
    idx = style_scorer.load_ref_index(ROOT)
    if idx is None:
        pytest.skip("local customer reference index absent")
    assert len(idx["ids"]) == 130 and len(idx["style"]) == 4
