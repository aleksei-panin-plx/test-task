"""Stage 4 set-level checks: CSD style consistency and the VLM 5×2 sheet review.

Offline only: synthetic vectors, FakeLLM for the sheet reviewer, httpx.MockTransport and a
temporary ledger for the OpenRouter adapter. No model is loaded and no network is used.
"""

import json
import socket
import tomllib
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image

from cardforge.contracts.request import CollectionRequest, Hitl
from cardforge.evaluate import set_qa, sheet_review
from cardforge.evaluate.api_budget import Ledger
from cardforge.llm.fake import FakeLLM
from cardforge.llm.provider import seal
from cardforge.orchestrator import service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
CSD_INDEX = ROOT / set_qa.STYLE_INDEXES["csd"][0]
needs_index = pytest.mark.skipif(not CSD_INDEX.exists(), reason="local CSD index absent")


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def synthetic_reference(sets=8, size=10, dim=16, spread=0.15, seed=0) -> dict:
    rng = np.random.default_rng(seed)
    base = rng.normal(size=dim)
    ids, vectors, set_of = [], [], {}
    for s in range(sets):
        centre = base + rng.normal(scale=0.3, size=dim)
        for i in range(size):
            ids.append(f"s{s}_{i}")
            vectors.append(centre + rng.normal(scale=spread * np.linalg.norm(base), size=dim))
            set_of[ids[-1]] = s
    index = {
        "ids": ids,
        "vectors": [v.tolist() for v in vectors],
        "reference_centroid_distances_loo": [0.1] * len(ids),
    }
    return set_qa.reference_sets(index, set_of), base, rng


# ---------------------------------------------------------------- style consistency


def test_style_outlier_is_flagged_and_a_coherent_set_is_not():
    ref, base, rng = synthetic_reference()
    scale = 0.07 * np.linalg.norm(base)  # tighter than the real sets
    coherent = {f"c{i:02d}": (base + rng.normal(scale=scale, size=16)).tolist() for i in range(9)}
    result = set_qa.style_consistency(coherent, ref)
    assert result["status"] == "ok" and result["outliers"] == []
    assert result["dispersion_ok"]
    foreign = dict(coherent, c09=(-base + rng.normal(scale=scale, size=16)).tolist())
    result = set_qa.style_consistency(foreign, ref)
    assert result["outliers"] == ["c09"]
    assert result["cards"]["c09"]["outlier"] and result["cards"]["c09"]["robust_z"] >= 2
    assert result["thresholds"]["outlier_percentile"] == 95.0


def test_style_thresholds_come_from_the_config_and_small_sets_are_not_run():
    ref, base, rng = synthetic_reference()
    cards = {f"c{i}": (base + rng.normal(size=16)).tolist() for i in range(3)}
    assert set_qa.style_consistency(cards, ref)["status"] == "not_run"
    cfg = tomllib.loads((ROOT / "config/runtime.toml").read_text())["set_qa"]
    assert cfg["style_metric"] == "csd"
    for key in ("outlier_percentile", "outlier_robust_z", "dispersion_percentile", "min_cards"):
        assert key in cfg


def test_reference_sets_need_five_real_sets():
    index = {
        "ids": [f"a{i}" for i in range(8)],
        "vectors": np.eye(8).tolist(),
        "reference_centroid_distances_loo": [0.1] * 8,
    }
    with pytest.raises(ValueError, match="five real sets"):
        set_qa.reference_sets(index, {f"a{i}": i // 4 for i in range(8)})


@needs_index
def test_csd_reference_is_built_from_the_stored_index_without_a_model():
    ref = set_qa.load_reference(ROOT, "csd")
    assert ref["sets"] >= 10 and len(ref["loo"]) == sum(ref["set_sizes"])
    assert ref["model"]["model_id"] == "tomg-group-umd/CSD-ViT-L"


# ---------------------------------------------------------------- sheet and answer


def task_for(tmp_path, n=4) -> dict:
    images = []
    for i in range(n):
        p = tmp_path / f"c{i}.png"
        Image.new("RGB", (430, 480), (40 * i, 100, 200)).save(p)
        images.append(p)
    sheet = sheet_review.labeled_sheet(tmp_path / "sheet.png", images)
    with Image.open(sheet) as im:
        assert im.size == (sheet_review.TILE[0] * 5, sheet_review.TILE[1] * 2)
    from cardforge.evaluate.common import sha256

    return seal(
        sheet_review.sheet_task(
            variant_id="v01",
            sheet="sheet.png",
            sheet_sha256=sha256(sheet),
            cards=[
                {"position": i + 1, "card_id": f"v01_c{i + 1:02d}", "object": "x", "category": 1}
                for i in range(n)
            ],
        )
    )


def test_sheet_answer_maps_numbers_to_card_ids_and_drops_unknown_numbers(tmp_path):
    task = task_for(tmp_path)
    answer = sheet_review.fake_answer(task, [2, 9])
    result = sheet_review.sheet_outcome(answer, task, "fake", "fake-model")
    assert result["status"] == "ok" and not result["consistent_set"]
    assert result["outliers"] == ["v01_c02"]
    assert result["invalid_positions"] == [9]
    assert result["aspects"]["palette"]["outliers"] == ["v01_c02"]
    bad = sheet_review.sheet_outcome({"palette": {}}, task, "fake", "m")
    assert bad["status"] == "invalid"


def test_sheet_prompt_keeps_planned_background_variety_out_of_outliers():
    assert "Цвета и мотивы фонов меняются по плану" in sheet_review.PROMPT
    schema = sheet_review.SheetAnswer.model_json_schema()
    assert set(sheet_review.ASPECTS) <= set(schema["properties"])


# ---------------------------------------------------------------- OpenRouter adapter


class Mock:
    def __init__(self, text: str, status: int = 200):
        self.bodies: list[dict] = []
        self.text = text
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
                "usage": {"prompt_tokens": 2100, "completion_tokens": 1200, "cost": 0.0162},
            },
        )


def test_openrouter_sheet_review_pins_the_route_accounts_and_reuses(tmp_path):
    cfg = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]
    kimi = next(c for c in cfg["candidates"] if c["name"] == "kimi-k3.1-pro")
    ledger = Ledger(tmp_path / "ledger.json", 5.0, 5.0)
    (tmp_path / "run").mkdir()
    task = task_for(tmp_path / "run")
    mock = Mock(json.dumps(sheet_review.fake_answer(task, [3]), ensure_ascii=False))
    provider = sheet_review.OpenRouterSheetProvider(
        ROOT,
        route=kimi["provider"],
        transport=httpx.MockTransport(mock),
        settings_override=(cfg, kimi, ledger),
        api_key="test-key-not-real",
    )
    store = RunStore(tmp_path / "run")
    reply = provider.ask(store, "set_sheet_v01", task)
    body = mock.bodies[0]
    assert body["provider"] == {
        "only": [kimi["provider"]],
        "allow_fallbacks": False,
        "require_parameters": True,
        "zdr": True,
        "data_collection": "deny",
        "ignore": cfg["forbidden_providers"],
    }
    assert body["response_format"]["json_schema"]["schema"] == (
        sheet_review.SheetAnswer.model_json_schema()
    )
    sent = json.dumps(body["messages"], ensure_ascii=False)
    assert "approval" not in sent and "human" not in sent  # plan facts and the sheet only
    assert reply.provider == f"openrouter:{kimi['provider']}" and reply.model == kimi["model"]
    assert reply.data["_meta"]["provider_reported"] == "Moonshot AI"
    raw = json.loads(store.path("sets/sheet-review/responses/set_sheet_v01.raw.json").read_text())
    assert raw["model_reported"] == kimi["model"] and raw["provider_pinned"] == kimi["provider"]
    summary = ledger.summary()
    assert summary["requests"] == 1 and summary["unsettled"] == 0
    assert Decimal(summary["accounted_upper_usd"]) >= Decimal("0.0162")
    again = provider.ask(store, "set_sheet_v01", task)  # saved answer, no second request
    assert len(mock.bodies) == 1 and again.cost_usd == 0
    result = sheet_review.sheet_outcome(reply.data, task, reply.provider, reply.model)
    assert result["outliers"] == ["v01_c03"]


def test_openrouter_sheet_review_estimate_is_the_configured_ceiling():
    cfg = tomllib.loads((ROOT / "config/runtime.toml").read_text())["set_qa"]["sheet"]
    ev = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]
    kimi = next(c for c in ev["candidates"] if c["name"] == cfg["candidate"])
    provider = sheet_review.OpenRouterSheetProvider(
        ROOT,
        route=cfg["route"],
        max_input_tokens_ceiling=cfg["max_input_tokens_ceiling"],
        max_output_tokens=cfg["max_output_tokens"],
        settings_override=(ev, kimi, None),
    )
    estimate = provider.estimate_cost({})
    assert 0 < estimate <= 0.1 and cfg["max_cost_usd"] <= 1.0


# ---------------------------------------------------------------- orchestrator


def brief() -> CollectionRequest:
    return CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme="Вестерн",
        producer_wishes_raw="побольше ретро",
        render_profile="fake",
        hitl=Hitl(g1="none", g2="none"),
    )


def new_run(ws, run_id, **options) -> str:
    return service.create_run(
        ws, brief(), llm="fake", render="fake", judge="fake", shadow_judges=[],
        run_id=run_id, runtime_options=options,
    )  # fmt: skip


def test_fake_run_reviews_each_finished_sheet_once(ws, no_network):
    rid = new_run(ws, "sheet", fake_sheet_outliers=[2])
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    data = store.read_json("sets/v01.json")
    review = data["sheet_review"]
    assert review["status"] == "ok" and review["provider"] == "fake"
    assert len(review["outliers"]) == 1 and data["decision"] in ("warn", "fail")
    assert any(n.startswith("sheet outlier") for n in data["notes"])
    assert store.exists("sets/v01.sheet-review.png")
    assert data["style_consistency"] is None  # FakeBackend: no style embedder
    calls = [e for e in store.events() if e.type == "llm_call" and "set_sheet" in e.payload["key"]]
    service.export(ws, rid)
    again = [e for e in store.events() if e.type == "llm_call" and "set_sheet" in e.payload["key"]]
    assert len(again) == len(calls) and calls  # unchanged set: never asked twice
    assert "sheet outlier" in store.path("report.md").read_text()


@needs_index
def test_style_check_runs_in_the_orchestrator_with_an_injected_embedder(
    ws, monkeypatch, no_network
):
    centroid = np.asarray(set_qa.load_reference(ROOT, "csd")["centroid"])
    rng = np.random.default_rng(1)
    seen: list[int] = []

    def embed(paths):
        seen.append(len(paths))
        out = []
        for p in paths:
            far = "v01_c03" in str(p)
            v = (-centroid if far else centroid) + rng.normal(scale=0.01, size=centroid.size)
            out.append(v.tolist())
        return out

    monkeypatch.setattr(
        service, "_set_qa_runtime", lambda ws, opts, fake: (embed, "test", FakeLLM())
    )
    rid = new_run(ws, "style")
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    style = store.read_json("sets/v01.json")["style_consistency"]
    if "v01_c03" in style["cards"]:
        assert style["outliers"] == ["v01_c03"]
    assert style["status"] == "ok" and style["embedder"] == "test"
    before = sum(seen)
    service.export(ws, rid)
    assert sum(seen) == before  # embeddings cached by image SHA


def test_sheet_review_repeat_is_a_separate_request(tmp_path):
    cfg = tomllib.loads((ROOT / "config/evaluation.toml").read_text())["openrouter"]
    kimi = next(c for c in cfg["candidates"] if c["name"] == "kimi-k3.1-pro")
    ledger = Ledger(tmp_path / "ledger.json", 5.0, 5.0)
    (tmp_path / "run").mkdir()
    task = task_for(tmp_path / "run")
    mock = Mock(json.dumps(sheet_review.fake_answer(task), ensure_ascii=False))
    store = RunStore(tmp_path / "run")

    def provider(repeat):
        return sheet_review.OpenRouterSheetProvider(
            ROOT,
            transport=httpx.MockTransport(mock),
            settings_override=(cfg, kimi, ledger),
            api_key="test-key-not-real",
            repeat=repeat,
        )

    provider(0).ask(store, "k", task)
    assert provider(0).ask(store, "k", task).cost_usd == 0  # same request: saved answer
    with pytest.raises(ValueError, match="another request"):
        provider(1).ask(store, "k", task)  # a resample is a new request, never a reuse
    assert len(mock.bodies) == 1 and ledger.summary()["requests"] == 1


# ---------------------------------------------------------------- set composition (object_count)


def _cards(categories, scales=("handheld", "furniture", "vehicle", "structure")):
    from types import SimpleNamespace as N

    colors = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "teal", "brown", "gray"]
    return [
        N(card_id=f"v15_c{i:02d}", category=cat, scale_class=scales[i % len(scales)],
          object_type=f"type{i}", background=N(motif="dots", color=colors[i]))  # fmt: skip
        for i, cat in enumerate(categories)
    ]


def test_set_check_follows_the_object_count_rule_of_the_plan_validator():
    """Set 15 (4 gold slots → 6 object cards) may miss one category, as the plan validator allows
    (grammar `object_count`, user 2026-11-10); 8 object cards still need all four."""
    from types import SimpleNamespace as N

    from cardforge.planner.validators import load_grammar

    grammar = load_grammar(ROOT)
    six = set_qa.check_set(N(variant_id="v15", cards=_cards([1, 1, 2, 2, 3, 3])), {}, grammar)
    assert six.decision != "fail" and not [n for n in six.notes if n.startswith("categories")]
    assert any("category ['4'] missing" in n for n in six.notes)
    def check(vid, cats):
        return set_qa.check_set(N(variant_id=vid, cards=_cards(cats)), {}, grammar)

    assert check("v15", [1, 1, 1, 2, 2, 2]).decision == "fail"  # two categories missing
    eight = check("v09", [1, 1, 2, 2, 2, 3, 3, 3])
    assert eight.decision == "fail" and eight.notes[0].startswith("categories")
    assert check("v01", [1, 1, 2, 2, 3, 3, 4, 4, 1, 2]).decision != "fail"
