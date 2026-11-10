"""Stage 11: autonomy hooks (TA §12.3) and `cardforge eval autonomy-readiness`.

Offline only: a synthetic project in tmp_path (decisions, labels, a run with costs); the socket
module is blocked to prove the report needs no network. The real `runs/` data is never touched.
"""

import json
import socket
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cardforge import autonomy
from cardforge.cli import app
from cardforge.evaluate import autonomy_readiness as ar
from cardforge.orchestrator.execution import stop_requested

ROOT = Path(__file__).resolve().parents[1]
GPT, NANO = "or-gpt-image-2-5", "or-nano-banana-v2"


# ---------------------------------------------------------------- hooks


def test_external_actions_off_by_default_in_project_config():
    cfg = autonomy.load_autonomy(ROOT)
    ext = cfg["external_actions"]
    assert ext["require_confirmation"] is True
    assert {a: ext[a] for a in autonomy.EXTERNAL_ACTIONS} == dict.fromkeys(
        autonomy.EXTERNAL_ACTIONS, False
    )
    for action in autonomy.EXTERNAL_ACTIONS:
        assert not autonomy.external_action_allowed(ROOT, action, confirmed=True)


def test_external_actions_off_without_config(tmp_path):
    cfg = autonomy.load_autonomy(tmp_path)
    assert not any(cfg["external_actions"][a] for a in autonomy.EXTERNAL_ACTIONS)
    assert cfg["audit"]["sample_share"] == 0.10
    assert not autonomy.external_action_allowed(tmp_path, "github", confirmed=True)
    assert not autonomy.external_action_allowed(tmp_path, "slack", confirmed=True)  # unknown


def test_enabled_action_still_needs_confirmation(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/autonomy.toml").write_text(
        "[external_actions]\nrequire_confirmation = false\ngithub = true\n"
    )
    cfg = autonomy.load_autonomy(tmp_path)
    assert cfg["external_actions"]["require_confirmation"] is True  # cannot be switched off
    assert not autonomy.external_action_allowed(tmp_path, "github")
    assert autonomy.external_action_allowed(tmp_path, "github", confirmed=True)
    assert not autonomy.external_action_allowed(tmp_path, "miro", confirmed=True)
    assert autonomy.describe(tmp_path)[0] == "warning"


def test_bad_external_action_config_is_an_error(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/autonomy.toml").write_text('[external_actions]\nmiro = "yes"\n')
    with pytest.raises(ValueError):
        autonomy.load_autonomy(tmp_path)
    (tmp_path / "config/autonomy.toml").write_text("[external_actions]\nslack = true\n")
    with pytest.raises(ValueError):
        autonomy.load_autonomy(tmp_path)
    assert autonomy.describe(tmp_path)[0] == "error"


def test_kill_switch_matches_the_orchestrator(tmp_path, monkeypatch):
    monkeypatch.delenv("CARDFORGE_STOP", raising=False)
    assert not autonomy.kill_switch_active(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/STOP").write_text("")
    assert autonomy.kill_switch_active(tmp_path) and stop_requested(tmp_path / "runs")
    (tmp_path / "runs/STOP").unlink()
    monkeypatch.setenv("CARDFORGE_STOP", "1")
    assert autonomy.kill_switch_active(tmp_path) and stop_requested(tmp_path / "runs")
    assert "ACTIVE" in autonomy.describe(tmp_path)[1]


def test_project_config_reads_only_local_files():
    raw = (ROOT / "config/autonomy.toml").read_text()
    assert "http://" not in raw and "https://" not in raw
    cfg = tomllib.loads(raw)
    for camp in cfg["readiness"]["campaigns"]:
        assert camp["sample"] in ("in_sample", "out_of_sample")
        for value in [camp["decisions"], *camp["labels"].values()]:
            assert not str(value).startswith("/")


# ---------------------------------------------------------------- statistics


def test_clopper_pearson_and_kappa():
    assert ar.clopper_pearson(5, 146) == [0.0112, 0.0781]  # README shadow-03, variant (a′)
    assert ar.clopper_pearson(0, 0) is None
    assert ar.clopper_pearson(0, 10)[0] == 0.0 and ar.clopper_pearson(10, 10)[1] == 1.0
    assert ar.kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert ar.kappa([True, True], [True, True]) is None


# ---------------------------------------------------------------- synthetic project


def dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


def decision(run, i, profile, *, ar_=False, ap=None, p=0.4, image=None, layers=None):
    shadow = {
        "layers": layers or {"hard": ar_, "step0": False, "style_profile": False},
        "auto_reject": ar_,
    }
    if ap is not None:
        shadow["auto_pass"] = ap
    return {
        "item_id": f"{run}:c{i:02d}:a01",
        "run": run,
        "profile": profile,
        "style_p": p,
        "image": image,
        "shadow": shadow,
    }


def build(root: Path, *, mandatory_fail=False) -> None:
    # in-sample campaign (y3 labels, auto-pass derived from τ_a): never used for the verdict
    ins = [decision("s1-a", i, GPT, p=0.2) for i in range(4)] + [
        decision("s1-a", 9, NANO, ar_=True, p=0.9)
    ]
    dump(root / "runs/s1/decisions.private.json", {"items": ins})
    dump(
        root / "runs/s1/y3.private.json",
        {
            "items": [
                {
                    "item_id": d["item_id"],
                    "y3": int(n == 0),
                    "reasons": ["material"] if n == 0 else [],
                }
                for n, d in enumerate(ins)
            ]
        },
    )
    # out-of-sample campaign: 3 briefs × 60 GPT images = 180; 100 journaled auto-pass, 8 auto-reject
    items, cards, ratings = [], [], {}
    for b in range(3):
        run = f"s2-{b}"
        for i in range(60):
            n = b * 60 + i
            img = None
            if n == 1:  # policy flag: journal says pass, runtime rule sends it to G2
                img = f"runs/s2/{run}/c{i:02d}/final.png"
                dump(
                    root / img.replace("final.png", "eval.json"),
                    {"policy_verdicts": [{"verdict": "uncertain"}]},
                )
            items.append(decision(run, i, GPT, ar_=n % 9 == 0 and n >= 100, ap=n < 100, image=img))
            card = f"k{n:03d}"
            cards.append(
                {"card": card, "item_id": items[-1]["item_id"], "kind": "first", "session": 1}
            )
            reject = n in (3, 50, 150) or (n >= 100 and n % 9 == 0 and n % 2 == 0)
            reasons = (["text"] if mandatory_fail and n == 3 else ["material"]) if reject else []
            ratings[card] = {"decision": "reject" if reject else "accept", "reasons": reasons}
    cards += [
        {"card": "ctl1", "item_id": "x", "kind": "control", "session": 1, "expected": "accept"},
        {"card": "rep1", "item_id": items[3]["item_id"], "kind": "repeat", "session": 1},
        {"card": "kr1", "item_id": "old", "kind": "known_reject", "session": 1},
    ]
    ratings |= {
        "ctl1": {"decision": "reject"},
        "rep1": {"decision": "reject"},
        "kr1": {"decision": "accept"},
    }
    dump(root / "runs/s2/decisions.private.json", {"items": items})
    dump(root / "runs/s2/mapping.private.json", {"cards": cards})
    dump(root / "runs/s2/labels.private/R1.json", {"ratings": ratings})
    dump(root / "runs/s2/extra.json", {"v2": {"added": [items[150]["item_id"]]}})
    # one run with costs: 10 positions, 8 accepted, 6 first attempt, $0.72 (one estimate-only)
    run = root / "runs/stage04/r1"
    cards_m = {
        f"c{i}": {"state": "accepted" if i < 8 else "rejected", "first_attempt_accepted": i < 6}
        for i in range(10)
    }
    dump(run / "manifest.json", {"render_profile": GPT, "state": "partial", "cards": cards_m})
    dump(
        run / "costs.json",
        {
            "calls": [{"kind": "render", "actual_usd": 0.08, "estimate_usd": 0.08}]
            + [{"kind": "render", "actual_usd": 0.064, "estimate_usd": 0.08}] * 10
        },
    )
    (run / "events.jsonl").write_text(json.dumps({"type": "auto_pass"}) + "\n")
    (root / "runs/unlabelled").mkdir()
    dump(root / "runs/unlabelled/decisions.private.json", {"items": []})
    (root / "config").mkdir(exist_ok=True)
    (root / "config/autonomy.toml").write_text("""
[readiness.scan]
runs_glob = ["runs/stage04/*/manifest.json"]
decisions_glob = ["runs/**/decisions.private.json"]
blind_glob = ["runs/stage09/**/*.json"]
[[readiness.campaigns]]
id = "s1"
decisions = "runs/s1/decisions.private.json"
sample = "in_sample"
labels = { kind = "y3", path = "runs/s1/y3.private.json", labeler = "R1" }
auto_pass_rule = { profile = "or-gpt-image-2-5", tau_a = 0.5 }
[[readiness.campaigns]]
id = "s2"
decisions = "runs/s2/decisions.private.json"
sample = "out_of_sample"
labels = { kind = "blind", mapping = "runs/s2/mapping.private.json", \
ratings = "runs/s2/labels.private/R1.json", labeler = "R1" }
extra_auto_pass = { path = "runs/s2/extra.json", key = "v2.added" }
""")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("autonomy readiness must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_readiness_report_on_synthetic_runs(tmp_path, no_network):
    build(tmp_path)
    rep = ar.autonomy_readiness(tmp_path)
    assert rep["network"] is False
    # in-sample: derived auto-pass (4 GPT with p ≤ τ_a), 1 false — reported, not used
    s1 = rep["by_campaign"]["s1"][GPT]["auto_pass"]
    assert s1["false_pass"]["k"] == 1 and s1["false_pass"]["n"] == 4
    m = rep["out_of_sample"][GPT]
    assert m["n"] == 180 and m["briefs"] == 3
    # journal (a) 100; (a′) +1 extra; runtime rule drops the policy-flag card → 100
    assert m["auto_pass_journal"]["share"]["k"] == 100
    assert m["auto_pass_journal_v2"]["share"]["k"] == 101
    assert m["auto_pass"]["share"]["k"] == 100 and m["auto_pass"]["policy_flag_passed"] == 0
    assert m["auto_pass"]["false_pass"]["k"] == 3  # items 3, 50 and the (a′) item 150
    assert m["auto_pass"]["human_review_share"]["k"] == 80
    assert rep["verdicts"]["policies"]["observed"]["policy_flag_in_shadow_journal"] == 1
    v = rep["verdicts"]["by_profile"][GPT]
    assert v["auto_pass"]["status"] == "ready_flagged"
    assert v["auto_reject"]["status"] == "not_ready"  # 8 auto-rejects, 4 accepted by the human
    assert rep["verdicts"]["policies"]["status"] == "not_ready"
    assert "полнота проверок не измерена" in rep["verdicts"]["policies"]["why"]
    assert rep["verdicts"]["blind_test"]["status"] == "not_run"
    # cost: $0.72 / 8 accepted; the estimate-only render is shown separately; first attempt 6/10
    cost = rep["costs"]["by_profile"][GPT]
    assert cost["usd_per_accepted"] == 0.09 and cost["usd_per_accepted_excl_estimates"] == 0.08
    assert cost["first_attempt_of_positions"]["rate"] == 0.6
    assert rep["verdicts"]["g3_monitor"]["real_auto_passed"] == 1
    q = rep["campaigns"][1]["label_quality"]
    assert q["controls"]["1"] == {"n": 1, "miss": 1} and q["retest"]["pairs"] == 1
    assert q["known_rejects_rejected_again"]["k"] == 0
    assert rep["overall"]["g2_flagged"] == [GPT] and rep["overall"]["none_audit"] == "not_ready"
    assert any("слепой тест" in b for b in rep["overall"]["none_audit_blockers"])
    assert rep["unlabelled_decisions"] == ["runs/unlabelled/decisions.private.json"]
    assert any("один разметчик" in c for c in rep["caveats"])
    # labels never leave as content: only ids and counts, no rating payloads
    assert "decision" not in json.dumps(rep["by_campaign"])


def test_mandatory_reason_blocks_auto_pass(tmp_path, no_network):
    build(tmp_path, mandatory_fail=True)
    v = ar.autonomy_readiness(tmp_path)["verdicts"]["by_profile"][GPT]["auto_pass"]
    assert v["status"] == "not_ready" and "ta_mandatory_missed" in v["why"]


def test_blind_test_is_read_when_present(tmp_path, no_network):
    build(tmp_path)
    dump(
        tmp_path / "runs/stage09/blind/score.json",
        {"trials": 30, "target_recall": 0.33, "control_all_real": False, "family": "x"},
    )
    assert ar.autonomy_readiness(tmp_path)["verdicts"]["blind_test"]["status"] == "pass"
    dump(
        tmp_path / "runs/stage09/blind/score.json",
        {"trials": 30, "target_recall": 0.6, "control_all_real": False},
    )
    assert ar.autonomy_readiness(tmp_path)["verdicts"]["blind_test"]["status"] == "fail"


def test_cli_writes_the_report(tmp_path):
    build(tmp_path)
    out = tmp_path / "report.json"
    result = CliRunner().invoke(
        app, ["eval", "autonomy-readiness", "--root", str(tmp_path), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["format"] == ar.FORMAT


def test_missing_input_is_a_clear_error(tmp_path):
    build(tmp_path)
    (tmp_path / "runs/s2/labels.private/R1.json").unlink()
    result = CliRunner().invoke(app, ["eval", "autonomy-readiness", "--root", str(tmp_path)])
    assert result.exit_code == 1 and "missing input" in result.output
