# ruff: noqa: E501
"""What the server copy of the core would send to OpenRouter (offline: httpx.MockTransport, a
temporary ledger, no key, $0).

1. Every formed judge request pins one provider, `allow_fallbacks=false`, `zdr=true`,
   `data_collection="deny"` — except the GPT judge, which runs without ZDR by the user's
   decision of 2026-11-04 (OpenAI has no ZDR endpoint on OpenRouter). Render and GPT-edit
   requests carry `allow_fallbacks=false` and `data_collection="deny"`; ZDR is a judge rule
   (AGENTS.md 2026-11-02) and render requests do not carry it — see the strict xfail below.
2. Human labels and gate decisions never reach a judge payload: a server run with human G1/G2
   decisions full of markers, plus `*.private*` files, then every captured judge body is searched.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from cardforge.contracts.generation import PromptBundle, RenderParams
from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.evaluate import openrouter_judge, veto_judge
from cardforge.evaluate.api_budget import Ledger
from cardforge.evaluate.batch_judges import OpenRouterAbsoluteJudge
from cardforge.orchestrator import product, service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace, render_profile
from cardforge.render import edit_pass
from helpers import FOLDER
from server import core_hooks

EVAL = tomllib.loads((FOLDER / "config/evaluation.toml").read_text(encoding="utf-8"))["openrouter"]
RUNTIME = tomllib.loads((FOLDER / "config/runtime.toml").read_text(encoding="utf-8"))
PRISTINE_INIT = veto_judge.OpenRouterVetoProvider.__init__
MARKERS = ("МЕТКА-ЧЕЛОВЕКА-7731", "Оксана-R1-7731", "SECRET-PRIVATE-7731", "G1-ОТВЕТ-7731")


def _judge_rule(provider: dict) -> None:
    assert provider["allow_fallbacks"] is False
    assert provider["data_collection"] == "deny"
    assert provider["zdr"] is True
    assert provider["require_parameters"] is True
    assert len(provider["only"]) == 1


def test_every_openrouter_judge_candidate_body_pins_zdr_and_no_fallbacks():
    content = [{"type": "text", "text": "x"}]
    assert EVAL["candidates"]
    for cand in EVAL["candidates"]:
        body = openrouter_judge.request_body(EVAL, cand, content)
        _judge_rule(body["provider"])
        assert set(EVAL["forbidden_providers"]) <= set(body["provider"]["ignore"])


def test_gpt_judge_is_the_only_one_without_zdr():
    """The core keeps zdr=True by default even for it; the exception is switched on explicitly
    (`zdr=False`, decision 2026-11-04) and the server flow does not use this judge at all."""
    gpt = OpenRouterAbsoluteJudge(FOLDER, zdr=False)
    body = openrouter_judge.request_body(EVAL, gpt.candidate, [{"type": "text", "text": "x"}])
    p = gpt.provider_settings(body)["provider"]
    assert "zdr" not in p and p["allow_fallbacks"] is False and p["data_collection"] == "deny"
    assert p["only"] == ["openai"]  # its own provider let through, nothing else
    zdr_kept = OpenRouterAbsoluteJudge(FOLDER).provider_settings(
        openrouter_judge.request_body(EVAL, gpt.candidate, []))["provider"]  # fmt: skip
    assert zdr_kept["zdr"] is True


def _bundle(profile) -> PromptBundle:
    return PromptBundle(backend="openrouter-image", model_id=profile.model_id, prompt_text="p",
                        params=RenderParams(width=864, height=960, seed=7), template_version="t").sealed()  # fmt: skip


def _render_profiles(ws):
    return [render_profile(ws, n) for n, p in RUNTIME["render_profiles"].items()
            if p.get("backend") == "openrouter-image" and p.get("enabled", True)]  # fmt: skip


def test_render_and_edit_requests_deny_data_collection_without_fallbacks():
    ws = Workspace(root=FOLDER, runs_dir=FOLDER / "runs", refs_dir=Path("dataset"))
    profiles = _render_profiles(ws)
    assert {p.id for p in profiles} >= {"or-gpt-image-2-5", "or-nano-banana-v2"}
    for profile in profiles:
        backend = service._backend(ws, profile, {}).backend
        p = backend.request_body(_bundle(profile), EVAL)["provider"]
        assert p["allow_fallbacks"] is False and p["data_collection"] == "deny", profile.id
        if not profile.allow_openai:
            assert "openai" in p["ignore"], profile.id
    for arm in ("gpt", "nano"):
        b = edit_pass.make_backend(FOLDER, arm, source_png=b"\x89PNG")
        bundle = edit_pass.edit_bundle(arm, [], 1)
        p = b.request_body(bundle, EVAL)["provider"]
        assert p["allow_fallbacks"] is False and p["data_collection"] == "deny" and len(p["only"]) == 1


@pytest.mark.xfail(strict=True, reason="рендер-профили не ставят zdr (правило ZDR в AGENTS.md — для судей); "
                   "если пользователь распространит ZDR на не-OpenAI рендер, нужна правка ядра (zdr = true в профиле)")
def test_non_openai_render_requests_carry_zdr():
    ws = Workspace(root=FOLDER, runs_dir=FOLDER / "runs", refs_dir=Path("dataset"))
    for profile in _render_profiles(ws):
        if profile.model_id.startswith("openai/"):
            continue
        p = service._backend(ws, profile, {}).backend.request_body(_bundle(profile), EVAL)["provider"]
        assert p.get("zdr") is True, profile.id


# ---------------------------------------------------------------- judge payloads


def _reply() -> str:
    answer = {"extra_items": []}
    for k in veto_judge.V2.defects:
        answer[k] = {"defect": False, "note": "нет"}
    return json.dumps(answer, ensure_ascii=False)


class Capture:
    def __init__(self):
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(200, json={
            "id": f"gen-{len(self.bodies)}", "model": body["model"], "provider": "Moonshot AI",
            "choices": [{"message": {"content": _reply()}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2900, "completion_tokens": 900, "cost": 0.01},
        })  # fmt: skip


def test_human_labels_and_gate_decisions_never_reach_judge_payloads(tmp_path, monkeypatch):
    capture = Capture()
    cand = next(c for c in EVAL["candidates"] if c["name"] == "kimi-k3.1-pro")
    ledger = Ledger(tmp_path / "ledger.json", 5.0, 5.0)

    def init(self, root, **kw):
        PRISTINE_INIT(self, root, **kw, transport=httpx.MockTransport(capture), settings_override=(EVAL, cand, ledger))

    monkeypatch.setattr(veto_judge.OpenRouterVetoProvider, "__init__", init)
    # References are public customer art (user 2026-11-08) and not under test here; the data
    # asset is not needed for this check.
    monkeypatch.setattr(veto_judge, "references", lambda *a, **k: [])
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("CARDFORGE_WORKER", "inline")
    ws = Workspace(root=FOLDER, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))
    request = CollectionRequest(request_id="t", collection_theme="Кино", set_theme="",
                                producer_wishes_raw="побольше ретро", plan_mode="collection", sets_total=1, variants_count=1,
                                render_profile="fake", hitl=Hitl(g1="all", g2="all"),
                                budget=Budget(max_cost_usd=2.0))  # fmt: skip
    with core_hooks.acting_as(MARKERS[1]):
        core_hooks.install(_NoQueue())
        try:
            rid = service.create_run(ws, request, llm="fake", render="fake", shadow_judges=["kimi-veto-v2"], run_id="pay")
        finally:
            core_hooks.uninstall()
    store = RunStore(ws.run_dir(rid))
    assert store.manifest().owner == MARKERS[1] and store.manifest().deployment == "server"
    service.plan(ws, rid)
    # human labels and private files next to the run (as a labeling campaign would leave them)
    store.write("labels.private/r1.json", {"label": MARKERS[2]})
    store.write("review/human.private.json", {"note": MARKERS[2]})
    product.approve_plan(ws, rid, MARKERS[1], MARKERS[3])
    assert store.manifest().state == "awaiting_g2"
    before = len(capture.bodies)
    assert before > 0  # the shadow veto judge was asked about every first attempt
    gate = store.read_json("review/g2.json")
    items = [i["card_id"] for i in gate["items"]]
    decisions = {cid: "accept" for cid in items}
    decisions[items[0]] = f"fix: {MARKERS[0]} ручка короче"
    decisions[items[1]] = "reject"
    product.decide_cards(ws, rid, MARKERS[1], decisions)
    assert len(capture.bodies) > before  # the redraw was judged again, after the human decisions
    for body in capture.bodies:
        _judge_rule(body["provider"])
        text = json.dumps(body, ensure_ascii=False)
        for marker in MARKERS:
            assert marker not in text, marker
        assert "human_decision" not in text and "reviewer" not in text


class _NoQueue:
    def enqueue(self, run_id):  # the run is advanced inline by the test
        pass
