"""Stage 4, phase 2 of the judge autonomy strategy: Kimi «veto only» as a shadow judge.

Offline only: FakeLLM for the fake veto judge, httpx.MockTransport and a temporary ledger for
the OpenRouter adapter. The shadow answer is saved per attempt and never changes a decision.
"""

import json
import tomllib
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.evaluate import veto_judge
from cardforge.evaluate.api_budget import Ledger
from cardforge.orchestrator import service
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace, judge_profile
from cardforge.prompting import fixes

ROOT = Path(__file__).resolve().parents[1]
EXP1 = ROOT / veto_judge.FROZEN_PLAN
PRISTINE_INIT = veto_judge.OpenRouterVetoProvider.__init__
needs_refs = pytest.mark.skipif(
    not (ROOT / "dataset/CardC6_card_17.png").exists(), reason="local reference cards absent"
)


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


def brief(**kw) -> CollectionRequest:
    return CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme="Вестерн",
        producer_wishes_raw="побольше ретро",
        render_profile="fake",
        hitl=kw.pop("hitl", Hitl(g1="none", g2="all")),
        **kw,
    )


def run(ws, run_id, shadows, request=None, **options) -> str:
    return service.create_run(
        ws, request or brief(), llm="fake", render="fake", shadow_judges=shadows,
        run_id=run_id, runtime_options=options,
    )  # fmt: skip


def card_states(ws, rid) -> dict:
    return {c: (m.state, m.attempts) for c, m in RunStore(ws.run_dir(rid)).manifest().cards.items()}


# ---------------------------------------------------------------- frozen exp1 contract


@pytest.mark.skipif(not EXP1.exists(), reason="experiment 1 plan absent")
def test_prompt_schema_and_rule_are_the_frozen_experiment_1_ones(ws):
    plan = json.loads(EXP1.read_text())
    assert plan["checklist_prompt"] == veto_judge.CHECKLIST_PROMPT
    assert plan["checklist_schema"] == veto_judge.ChecklistAnswer.model_json_schema()
    assert plan["rules"][veto_judge.RULE] == "accept iff no checklist defect"
    cfg = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]
    kimi = next(c for c in cfg["candidates"] if c["name"] == "kimi-k3.1-pro")
    jp = judge_profile(ws, "kimi-veto")
    assert (jp.kind, jp.provider, jp.model, jp.route) == (
        "veto", "openrouter", kimi["model"], kimi["provider"],
    )  # fmt: skip
    assert jp.profile == veto_judge.PROFILE and jp.reasoning_effort == plan["reasoning_effort"]
    assert jp.max_cost_usd and not jp.panel and not jp.test_only


def test_every_defect_maps_to_a_fix_library_code():
    library = fixes.load_library(ROOT)
    assert set(veto_judge.DEFECT_CODES) == set(veto_judge.DEFECTS)
    assert set(veto_judge.DEFECT_CODES.values()) <= set(library.prompt_codes(list(library.codes)))


@needs_refs
@pytest.mark.skipif(not EXP1.exists(), reason="experiment 1 items absent")
def test_references_reproduce_experiment_1():
    items = json.loads((EXP1.parent / "items.json").read_text())
    for it in items[:: max(1, len(items) // 12)]:
        assert veto_judge.references(ROOT, it["id"], it["image_sha256"], it["spec"]) == it["refs"]


def test_outcome_maps_defects_to_codes_and_invalid_to_flag():
    answer = {"extra_items": ["кактус"]}
    for k in veto_judge.DEFECTS:
        answer[k] = {"defect": k in ("extra_props", "overload", "cropped"), "note": "n"}
    outcome, record = veto_judge.veto_outcome(answer, "openrouter:x", "m")
    assert outcome.decision == "reject" and record["veto"] is True
    assert record["codes"] == ["props", "crop"] and record["not_reject"] is False
    assert outcome.issues[0].startswith("extra_props [props]")
    clean = {**answer, **{k: {"defect": False, "note": "n"} for k in veto_judge.DEFECTS}}
    assert veto_judge.veto_outcome(clean, "p", "m")[0].decision == "accept"
    bad, rec = veto_judge.veto_outcome({"invalid": "not json"}, "p", "m")
    assert bad.decision == "flag" and rec["valid"] is False and rec["veto"] is None


# ---------------------------------------------------------------- core: shadow only


@needs_refs
def test_fake_veto_is_logged_per_attempt_and_never_changes_decisions(ws):
    plain = run(ws, "plain", [])
    shadow = run(ws, "shadow", ["fake-veto"])
    for rid in (plain, shadow):
        assert service.advance(ws, rid)["state"] == "awaiting_g2"
    assert card_states(ws, plain) == card_states(ws, shadow)
    store = RunStore(ws.run_dir(shadow))
    records = sorted(ws.run_dir(shadow).glob("cards/*/*/attempt_*/judges/fake-veto.json"))
    assert len(records) == 30
    vetoes = 0
    for path in records:
        rec = json.loads(path.read_text())
        assert rec["role"] == "shadow" and rec["profile"] == veto_judge.PROFILE
        assert rec["veto"]["rule"] == veto_judge.RULE and len(rec["refs"]) == 3
        assert set(rec["veto"]["codes"]) <= set(veto_judge.DEFECT_CODES.values())
        vetoes += rec["veto"]["veto"]
        ev = json.loads((path.parents[1] / "eval.json").read_text())
        assert ev["decision"] == "flag" and ev["judge"] is None
        assert ev["shadow_judges"]["fake-veto"]["decision"] in ("accept", "reject")
    assert 0 < vetoes < 30
    g2 = store.read_json("review/g2.json")
    assert all(i["judge_decision"] is None for i in g2["items"])  # the shadow stays hidden


@needs_refs
def test_fake_veto_look_flag_logs_look_items_and_look_signal(ws):
    """Look items behind the profile flag: same contract, signal only; the look signal of
    card QA is in every eval.json and never changes the decisions."""
    plain = run(ws, "plain", [])
    look = run(ws, "look", ["fake-veto-look"])
    for rid in (plain, look):
        assert service.advance(ws, rid)["state"] == "awaiting_g2"
    assert card_states(ws, plain) == card_states(ws, look)
    records = sorted(ws.run_dir(look).glob("cards/*/*/attempt_*/judges/fake-veto-look.json"))
    assert records
    for path in records:
        rec = json.loads(path.read_text())
        assert rec["profile"] == veto_judge.LOOK_PROFILE
        assert rec["veto"]["profile"] == veto_judge.LOOK_PROFILE
        assert rec["veto"]["look_defects"] == [] and rec["veto"]["valid"] is True
        ev = json.loads((path.parents[1] / "eval.json").read_text())
        assert ev["hard_checks"]["look"]["signal_only"] is True
        assert ev["hard_checks"]["look"]["passed"] is None
    assert not judge_profile(ws, "kimi-veto-look").enabled_in_config  # off by default


def test_veto_judge_cannot_decide_and_needs_a_budget(ws):
    with pytest.raises(service.ServiceError, match="shadow-only"):
        service.create_run(ws, brief(), llm="fake", render="fake", judge="fake-veto",
                           shadow_judges=[])  # fmt: skip
    with pytest.raises(service.ServiceError, match="shadow-only"):
        service.create_run(ws, brief(), llm="fake", render="fake", deciders=["kimi-veto"],
                           shadow_judges=[])  # fmt: skip
    with pytest.raises(service.ServiceError, match="run budget"):
        run(ws, "nobudget", ["kimi-veto"])
    assert "kimi-veto" not in service.default_panel(ws, "none", test_run=False)
    assert "fake-veto" not in service.default_panel(ws, "none", test_run=True)


# ---------------------------------------------------------------- OpenRouter adapter offline


def checklist_reply(defects=("cropped",)) -> str:
    answer = {"extra_items": []}
    for k in veto_judge.DEFECTS:
        answer[k] = {"defect": k in defects, "note": "видно" if k in defects else "нет"}
    return json.dumps(answer, ensure_ascii=False)


class Mock:
    def __init__(self, text=None, status=200):
        self.bodies: list[dict] = []
        self.text = text or checklist_reply()
        self.status = status

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "rate"})
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(self.bodies)}",
                "model": body["model"],
                "provider": "Moonshot AI",
                "choices": [{"message": {"content": self.text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2900, "completion_tokens": 1500, "cost": 0.0208},
            },
        )


def patched_provider(monkeypatch, tmp_path, mock: Mock) -> Path:
    cfg = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]
    kimi = next(c for c in cfg["candidates"] if c["name"] == "kimi-k3.1-pro")
    path = tmp_path / "ledger.json"
    ledger = Ledger(path, 5.0, 5.0)

    def init(self, root, **kw):
        PRISTINE_INIT(self, root, **kw, transport=httpx.MockTransport(mock),
                 settings_override=(cfg, kimi, ledger))  # fmt: skip

    monkeypatch.setattr(veto_judge.OpenRouterVetoProvider, "__init__", init)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    return path


@needs_refs
def test_kimi_veto_offline_pins_the_route_and_accounts_every_call(ws, tmp_path, monkeypatch):
    mock = Mock()
    ledger_path = patched_provider(monkeypatch, tmp_path, mock)
    request = brief(budget=Budget(max_cost_usd=2.0))
    rid = run(ws, "kimi", ["kimi-veto"], request)
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    assert len(mock.bodies) == 30
    body = mock.bodies[0]
    assert body["model"] == "moonshotai/kimi-k3.1-pro"
    assert body["provider"] == {
        "only": ["moonshotai/mxfp4"], "allow_fallbacks": False, "require_parameters": True,
        "zdr": True, "data_collection": "deny", "ignore": ["openai", "azure"],
    }  # fmt: skip
    assert body["reasoning"]["effort"] == "high" and body["max_tokens"] == 8192
    content = body["messages"][0]["content"]
    assert content[0]["text"] == veto_judge.CHECKLIST_PROMPT
    assert content[1]["text"].startswith("Спецификация карты (данные):\n")
    labels = [p["text"] for p in content if p["type"] == "text"][2:]
    assert labels == ["Карта", "Миниатюра 215×240", "Референс игры 1", "Референс игры 2",
                      "Референс игры 3"]  # fmt: skip
    store = RunStore(ws.run_dir(rid))
    rec = store.read_json("cards/v01/v01_c01/attempt_01/judges/kimi-veto.json")
    assert rec["veto"]["codes"] == ["crop"] and rec["outcome"]["decision"] == "reject"
    assert rec["provider"] == "openrouter:moonshotai/mxfp4" and rec["cost_usd"] == 0.0208
    assert rec["request"]["provider_reported"] == "Moonshot AI"
    assert store.exists("judging/kimi-veto/responses/judge.kimi-veto.v01_c01.a01.raw.json")
    entries = json.loads(ledger_path.read_text())["entries"].values()
    assert len(entries) == 30 and all(e["status"] != "reserved" for e in entries)
    committed = BudgetLedger(store, 2.0).committed("judge:kimi-veto")
    assert committed == pytest.approx(30 * 0.0208)
    ev = store.read_json("cards/v01/v01_c01/attempt_01/eval.json")
    assert ev["decision"] == "flag"  # G2 all, judge none: the veto changed nothing


@needs_refs
def test_kimi_veto_errors_and_judge_budget_never_stop_the_run(ws, tmp_path, monkeypatch):
    mock = Mock(status=429)
    patched_provider(monkeypatch, tmp_path, mock)
    rid = run(ws, "err", ["kimi-veto"], brief(budget=Budget(max_cost_usd=2.0)))
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    ev = store.read_json("cards/v01/v01_c01/attempt_01/eval.json")
    assert ev["shadow_judges"]["kimi-veto"].startswith("error: ValueError")
    assert not list(ws.run_dir(rid).glob("cards/*/*/attempt_*/judges/kimi-veto.json"))
    # The per-judge cap: one ≈ $0.098 reservation fits under $0.15, the second does not.
    mock2 = Mock()
    patched_provider(monkeypatch, tmp_path / "b", mock2)
    monkeypatch.setattr(Workspace, "config", _with_cap(Workspace.config, 0.15))
    rid2 = run(ws, "cap", ["kimi-veto"], brief(budget=Budget(max_cost_usd=2.0)))
    assert service.advance(ws, rid2)["state"] == "awaiting_g2"
    asked = list(ws.run_dir(rid2).glob("cards/*/*/attempt_*/judges/kimi-veto.json"))
    assert 1 <= len(asked) <= 6 and len(mock2.bodies) == len(asked)


def _with_cap(original, cap):
    def patched(self):
        cfg = original(self)
        cfg["judges"]["kimi-veto"]["max_cost_usd"] = cap
        return cfg

    return patched


@needs_refs
def test_saved_raw_response_is_reused_without_a_second_call(ws, tmp_path, monkeypatch):
    mock = Mock(text="not json at all")
    patched_provider(monkeypatch, tmp_path, mock)
    rid = run(ws, "reuse", [], brief(hitl=Hitl(g1="none", g2="all")))
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    jp = judge_profile(ws, "kimi-veto")
    provider = service._judge_llm(jp, {}, root=ROOT)
    spec = next(c for p in service._plans(service._context(ws, rid, None)) for c in p.cards)
    ctx = service._context(ws, rid, None)
    rel = "cards/v01/v01_c01/attempt_01"
    from cardforge.evaluate.common import sha256
    from cardforge.llm.provider import seal

    task = seal(service._veto_task(ctx, spec, 1, rel, sha256(store.path(f"{rel}/final.png")), jp))
    first = provider.ask(store, "k1", task)
    assert first.cost_usd == pytest.approx(0.0208) and "invalid" in first.data
    again = provider.ask(store, "k1", task)
    assert len(mock.bodies) == 1 and again.cost_usd == 0.0
    assert again.data["_meta"]["reused_saved_response"] is True
    outcome, record = veto_judge.veto_outcome(again.data, again.provider, again.model)
    assert outcome.decision == "flag" and record["valid"] is False
    assert Decimal(Ledger(tmp_path / "ledger.json", 5.0, 5.0).summary()["accounted_upper_usd"]) > 0
