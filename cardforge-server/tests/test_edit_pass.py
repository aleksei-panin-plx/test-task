"""Edit pass in the core (fable-review Q3, user 2026-11-06): offline, no network, $0."""

import io
from pathlib import Path

import pytest
from PIL import Image

from cardforge.contracts.generation import PromptBundle, Reference
from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.evaluate.common import sha256
from cardforge.orchestrator import service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace
from cardforge.render import edit_pass, edit_runtime
from cardforge.render.openrouter_image import openrouter_ledger

ROOT = Path(__file__).resolve().parents[1]
CFG = edit_runtime.load_config(ROOT)


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


def brief(usd: float) -> CollectionRequest:
    return CollectionRequest(
        request_id="t", collection_theme="Кино", set_theme="Вестерн",
        producer_wishes_raw="побольше ретро", render_profile="fake",
        hitl=Hitl(g1="none", g2="flagged"), budget=Budget(max_cost_usd=usd),
    )  # fmt: skip


def new_run(ws, run_id, usd=1.0, **options) -> str:
    return service.create_run(
        ws, brief(usd), llm="fake", render="fake", judge="none", shadow_judges=[],
        run_id=run_id, runtime_options=options,
    )  # fmt: skip


def attempt(ws, rid, card, n, name):
    store = RunStore(ws.run_dir(rid))
    return store.path(f"cards/{card.split('_')[0]}/{card}/attempt_{n:02d}/{name}")


def g2(ws, rid, card, reasons, text="fix"):
    decisions = {cid: "accept" for cid in service.status(ws, rid)["waiting"]}
    decisions[card] = text
    service.decide_gate(ws, rid, "g2", decisions, reviewer="p", reasons={card: reasons})


def test_config_matches_the_user_decision():
    assert CFG["enabled"] and CFG["arm"] == "gpt" and CFG["max_per_card"] == 1
    assert CFG["profiles"] == ["or-gpt-image-2-5"] and set(CFG["codes"]) == {"material", "style"}
    assert edit_pass.PROFILES[CFG["arm"]]["provider_only"] == ["openai"]


def test_only_material_or_style_rounds_qualify():
    assert edit_runtime.eligible_codes([{"codes": ["material"]}], CFG) == ["material"]
    assert edit_runtime.eligible_codes(
        [{"source": "auto_reject", "codes": ["style"]}, {"codes": ["material", "style"]}], CFG
    ) == ["style", "material"]
    assert edit_runtime.eligible_codes([{"codes": ["material", "props"]}], CFG) == []
    assert edit_runtime.eligible_codes([{"codes": ["style"]}, {"codes": ["crop"]}], CFG) == []
    assert edit_runtime.eligible_codes([{"codes": [], "text": "simpler"}], CFG) == []
    assert edit_runtime.eligible_codes([{"codes": ["material"], "text": "x"}], CFG) == []
    assert edit_runtime.eligible_codes([{"codes": ["material"], "text": "x"}],
                                       {**CFG, "allow_text": True}) == ["material"]  # fmt: skip
    assert edit_runtime.eligible_codes([], CFG) == []


def test_material_fix_gets_one_edit_then_an_ordinary_redraw(ws):
    rid = new_run(ws, "edit", fake_edit={"profiles": ["fake"], "cost_usd": 0.05})
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    card = "v01_c03"
    g2(ws, rid, card, ["material"])
    service.advance(ws, rid)
    b2 = PromptBundle.model_validate_json(attempt(ws, rid, card, 2, "bundle.json").read_text())
    assert b2.backend == "openrouter-image-edit" and b2.prompt_text == edit_pass.INSTRUCTION
    assert b2.template_version.startswith(edit_pass.INSTRUCTION_VERSION + "+src:")
    assert {r.role for r in b2.references} <= set(edit_pass.EDIT_ROLES)
    src = attempt(ws, rid, card, 1, "raw.png")
    with Image.open(src) as a, Image.open(attempt(ws, rid, card, 2, "raw.png")) as b:
        assert a.size == b.size  # same frame: shape and composition kept
    store = RunStore(ws.run_dir(rid))
    events = [e for e in store.events() if e.type == edit_runtime.EVENT]
    assert len(events) == 1
    p = events[0].payload
    assert p["status"] == "ok" and p["cost_usd"] == 0.05 and p["codes"] == ["material"]
    assert p["source_sha256"] == sha256(src) and p["bundle_hash"] == b2.hash
    assert p["raw_sha256"] == sha256(attempt(ws, rid, card, 2, "raw.png"))
    assert store.manifest().counters["edit"][card]["attempt_no"] == 2
    # Rejected again for material: the second time is the ordinary redraw.
    assert card in service.status(ws, rid)["waiting"]
    g2(ws, rid, card, ["material"])
    service.advance(ws, rid)
    b3 = PromptBundle.model_validate_json(attempt(ws, rid, card, 3, "bundle.json").read_text())
    assert b3.backend == "fake"
    assert len([e for e in store.events() if e.type == edit_runtime.EVENT]) == 1


def test_other_codes_and_runs_without_edit_redraw(ws):
    rid = new_run(ws, "props", fake_edit={"profiles": ["fake"]})
    service.advance(ws, rid)
    g2(ws, rid, "v01_c02", ["material", "props"])
    service.advance(ws, rid)
    b = PromptBundle.model_validate_json(attempt(ws, rid, "v01_c02", 2, "bundle.json").read_text())
    assert b.backend == "fake"
    rid = new_run(ws, "off")  # FakeBackend run without `fake_edit`: no edit pass
    service.advance(ws, rid)
    g2(ws, rid, "v01_c02", ["style"])
    service.advance(ws, rid)
    b = PromptBundle.model_validate_json(attempt(ws, rid, "v01_c02", 2, "bundle.json").read_text())
    assert b.backend == "fake"
    assert not [e for e in RunStore(ws.run_dir(rid)).events() if e.type == edit_runtime.EVENT]


def test_edit_respects_the_run_budget(ws):
    rid = new_run(ws, "poor", usd=0.0, fake_edit={"profiles": ["fake"], "cost_usd": 0.05})
    service.advance(ws, rid)
    g2(ws, rid, "v01_c02", ["style"])
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    assert store.manifest().counters["budget_exhausted"] is True
    assert not attempt(ws, rid, "v01_c02", 2, "raw.png").exists()
    assert not [e for e in store.events() if e.type == edit_runtime.EVENT]


def test_real_request_is_the_q3_gpt_edit():
    png = io.BytesIO()
    Image.new("RGB", (12, 16), "red").save(png, format="PNG")
    ep = edit_runtime.EditPass(ROOT, CFG)
    refs = [Reference(id="CardC6_card_17", role="style", path="dataset/CardC6_card_17.png")]
    bundle = ep.bundle(refs, 7, "ab" * 32)
    backend = edit_pass.make_backend(ROOT, CFG["arm"], png.getvalue(), CFG["provider_options"])
    cfg, _ = openrouter_ledger(ROOT)
    body = backend.request_body(bundle, cfg)
    assert body["model"] == "openai/gpt-image-2.5-sunburst" and body["quality"] == "medium"
    assert body["provider"]["only"] == ["openai"] and body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["data_collection"] == "deny"
    assert body["provider"]["options"] == {"openai": {"input_fidelity": "high"}}
    first = body["input_references"][0]["image_url"]["url"]
    assert first.endswith(edit_pass._data_url(png.getvalue()).split(",", 1)[1])
    assert len(body["input_references"]) == 2


def test_paid_lock_waits_is_reentrant_and_always_released(tmp_path):
    lock = tmp_path / "paid-run.lock"
    lock.mkdir()  # another session holds it
    waits = []

    def sleep(s):
        waits.append(s)
        lock.rmdir()  # the other session finishes

    with edit_runtime.paid_lock(lock, sleep=sleep, log=lambda m: None):
        assert lock.is_dir()
        with edit_runtime.paid_lock(lock, sleep=pytest.fail):  # same process: no wait
            pass
        assert lock.is_dir()
    assert waits == [30.0] and not lock.exists()
    with pytest.raises(RuntimeError), edit_runtime.paid_lock(lock):
        raise RuntimeError("boom")
    assert not lock.exists()
