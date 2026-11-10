"""Stage 4, phase 1 of the judge autonomy strategy: offline fix loop (no network, $0).

Crop hard check → fix codes → accumulated history → normalized human comments →
verification on the next attempt → blind G2 with reason codes → honest seed records.
"""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from cardforge.cli import app
from cardforge.contracts.generation import CardEvaluation, PromptBundle
from cardforge.contracts.request import CardSpec, CollectionRequest, Hitl
from cardforge.evaluate import card_qa
from cardforge.evaluate.object_mask import crop_check, fake_masker
from cardforge.export import g2_labels
from cardforge.export.review_page import BLIND_MAP, write_review_page
from cardforge.llm.fake import fake_plan
from cardforge.llm.provider import HostAgentProvider
from cardforge.orchestrator import service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace, render_profile
from cardforge.policy.loader import resolve_active
from cardforge.prompting import fixes
from cardforge.prompting.assemble import build_bundle, load_template

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def geometry_fails_hard(monkeypatch):
    """These tests exercise the redraw loop with `crop` / `safe_zone` as a convenient hard
    fail. Since 2026-11-09 (user «Да, по плану») they are flags in the project config; here
    they keep the old behaviour so the loop itself stays covered (flags: test_autonomy_*)."""
    monkeypatch.setattr(card_qa, "flag_only_checks", lambda mask_config=None: frozenset())


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


@pytest.fixture
def library() -> fixes.FixLibrary:
    return fixes.load_library(ROOT)


def brief(**kw) -> CollectionRequest:
    return CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme="Вестерн",
        producer_wishes_raw="побольше ретро",
        render_profile="fake",
        hitl=kw.pop("hitl", Hitl(g1="none", g2="none")),
        **kw,
    )


def new_run(ws, request=None, *, judge="fake", run_id="r1", **options) -> str:
    return service.create_run(
        ws, request or brief(), llm="fake", render="fake", judge=judge, shadow_judges=[],
        run_id=run_id, runtime_options=options,
    )  # fmt: skip


def bundle(ws, rid, card, attempt) -> PromptBundle:
    store = RunStore(ws.run_dir(rid))
    variant = card.split("_")[0]
    return store.read_model(f"cards/{variant}/{card}/attempt_{attempt:02d}/bundle.json",
                            PromptBundle)  # fmt: skip


def evaluation(ws, rid, card, attempt) -> CardEvaluation:
    store = RunStore(ws.run_dir(rid))
    variant = card.split("_")[0]
    return store.read_model(f"cards/{variant}/{card}/attempt_{attempt:02d}/eval.json",
                            CardEvaluation)  # fmt: skip


def spec() -> CardSpec:
    draft = fake_plan(0, "Вестерн")["cards"][2]
    return CardSpec(**draft, card_id="v01_c03", reference_roles={}, seed=7,
                    safe_zone={"top_pct": 0.14, "bottom_pct": 0.11, "side_pct": 0.08},
                    style_profile_id="reference_cards_v1")  # fmt: skip


def mask(box, size=(430, 480)) -> Image.Image:
    out = Image.new("L", size, 0)
    ImageDraw.Draw(out).rectangle(box, fill=255)
    return out


# ---------------------------------------------------------------- item 2: fix library


def test_fix_library_covers_the_blind_review_codes(library):
    path = ROOT / "scripts/stage04_blind_review.py"
    spec_ = importlib.util.spec_from_file_location("blind", path)
    blind = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(blind)
    assert set(library.codes) == set(blind.REASONS) | {"other"}
    for code, fix in library.codes.items():
        if code == "other":
            assert fix.prompt is None and fix.question is None
            continue
        assert fix.prompt and fix.question and fix.label
        assert not fixes.CYRILLIC.search(fix.prompt + fix.question)
    assert set(card_qa.CHECK_CODES.values()) <= set(library.codes)
    with pytest.raises(ValueError, match="unknown reason code"):
        library.check(["crop", "nonsense"])
    assert library.check(["crop", "props", "crop"]) == ["crop", "props"]


def test_fix_library_lines_pass_prompt_guard_and_compose(ws, library):
    """Codes alone make a fix: every line reaches the prompt without a guard hit."""
    history: list[dict] = []
    for code in library.prompt_codes(list(library.codes)):
        fixes.add(history, {"source": "judge", "attempt": 1, "codes": [code]})
    text = fixes.compose(history, library)
    assert text.count(".") >= fixes.MAX_LINES
    packs = resolve_active(ROOT, ["CN"], [])
    for code in library.prompt_codes(list(library.codes)):
        b = build_bundle(
            load_template(ROOT, "prompts/render/ref-v1.toml"),
            render_profile(ws, "or-gpt-image-2-5"),
            spec(), packs, [], seed=1, references=[], max_refs=6,
            fix_instruction=library.codes[code].prompt,
        )  # fmt: skip
        assert "Correction for this attempt: " + library.codes[code].prompt in b.prompt_text


# ---------------------------------------------------------------- item 1: crop check


def test_crop_check_on_synthetic_masks():
    inside = crop_check(mask([100, 100, 330, 380]), 1)
    assert inside["passed"] is True and inside["code"] == "crop"
    top = crop_check(mask([100, 0, 330, 300]), 2)
    assert top["passed"] is False and top["edges"] == ["top", "top_headroom"]
    feet = crop_check(mask([100, 100, 330, 479]), 3)
    # Bottom contact is measured but no longer flags (user 2026-11-09, `[crop] flag_edges`).
    assert feet["passed"] is True and feet["measured_edges"] == ["bottom"]
    side = crop_check(mask([0, 100, 200, 300]), 1)
    assert side["passed"] is True and side["measured_edges"] == ["side"]
    # Category 4: scenes reach the bottom and sides by design; only the top counts.
    scene = crop_check(mask([0, 100, 429, 479]), 4)
    assert scene["passed"] is True
    assert crop_check(mask([0, 0, 429, 479]), 4)["passed"] is False
    # Edge contact alone: 11 px of 430 is below 5 %; top headroom off (`frame={}`).
    assert crop_check(mask([200, 0, 210, 300]), 1, frame={})["passed"] is True
    # Top headroom (2026-11-06): a hanging string (5 px) is fine, an 11 px spire in the
    # star band up to the edge is a top crop.
    assert crop_check(mask([200, 0, 204, 300]), 1)["passed"] is True
    spire = crop_check(mask([200, 0, 210, 300]), 1)
    assert spire["passed"] is False and spire["edges"] == ["top_headroom"]
    unknown = crop_check(None, 1)
    assert unknown["passed"] is None and unknown["required"] is False
    assert crop_check(Image.new("L", (430, 480), 0), 1)["passed"] is None


def test_crop_failure_goes_to_fix_with_library_text_without_judge(ws, library):
    rid = new_run(ws, fake_mask={"crop": {"v01_c02": [1]}})
    assert service.advance(ws, rid)["state"] == "completed"
    first = evaluation(ws, rid, "v01_c02", 1)
    assert first.decision == "fix" and first.reasons == ["hard_check:crop"]
    assert first.judge is None  # no judge needed for a measured defect
    assert first.fixes == [{"source": "hard_check:crop", "codes": ["crop"]}]
    store = RunStore(ws.run_dir(rid))
    assert not store.path("cards/v01/v01_c02/attempt_01/judges").exists()
    second = bundle(ws, rid, "v01_c02", 2)
    assert "Correction for this attempt: " + library.codes["crop"].prompt in second.prompt_text
    # The fix is verified on attempt 2: the crop check passes and the inspector agrees.
    after = evaluation(ws, rid, "v01_c02", 2)
    assert after.hard_checks["crop"]["passed"] is True
    assert after.fix_checks["crop"]["resolved"] is True and after.decision == "accept"
    record = store.manifest().counters["fix"]["v01_c02"]
    assert record["format"] == fixes.FORMAT and record["entries"][0]["codes"] == ["crop"]
    assert record["retry"] == "new_seed"  # a hard failure redraws with a new seed
    events = [e for e in store.events() if e.type == "fix_recorded"]
    assert events[0].payload["codes"] == ["crop"] and events[0].card_id == "v01_c02"


def test_fake_masker_reads_candidate_paths(tmp_path):
    path = tmp_path / "cards/v01/v01_c03/attempt_02/cand_1/final.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (430, 480)).save(path)
    m = fake_masker({"crop": {"v01_c03": [2]}})(path)
    assert crop_check(m, 1)["passed"] is False


# ---------------------------------------------------------------- item 3: history


def test_legacy_fix_record_stays_verbatim(library):
    legacy = {"instruction": "можно один прожектор убрать", "same_seed": True,
              "from_human": True}  # fmt: skip
    history = fixes.entries(legacy)
    assert fixes.compose(history, library) == "можно один прожектор убрать"
    assert not fixes.needs_normalizing(history[0])  # old runs keep their prompt on resume
    assert fixes.entries({"instruction": None, "same_seed": False, "from_human": False}) == []
    fixes.add(history, {"source": "human", "attempt": 2, "codes": ["props"], "text": "проще"})
    assert len(history) == 2
    fixes.add(history, {"source": "human", "attempt": 3, "codes": ["props"], "text": "Проще."})
    assert len(history) == 2 and history[1]["attempts"] == [2, 3]  # de-duplicated


def test_legacy_manifests_of_stage04_runs_still_load(library):
    from cardforge.contracts.run import RunManifest

    for path in sorted((ROOT / "runs/stage04").glob("*/manifest.json")):
        manifest = RunManifest.model_validate_json(path.read_text())
        for record in manifest.counters.get("fix", {}).values():
            text = fixes.compose(fixes.entries(record), library)
            assert text == record.get("instruction")


def test_two_human_fixes_both_reach_the_third_attempt(ws):
    """kino-western-03 v01_c03: the second comment must not erase the first."""
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="acc")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    first = {cid: "accept" for cid in service.status(ws, rid)["waiting"]}
    first["v01_c03"] = "fix: можно один прожектор убрать"
    service.decide_gate(ws, rid, "g2", first, reviewer="panin")
    assert service.advance(ws, rid)["waiting"] == ["v01_c03"]
    service.decide_gate(ws, rid, "g2", {"v01_c03": "fix: идея хорошая, фон слишком яркий"},
                        reviewer="panin")  # fmt: skip
    service.advance(ws, rid)
    prompt = bundle(ws, rid, "v01_c03", 3).prompt_text
    store = RunStore(ws.run_dir(rid))
    entries = store.manifest().counters["fix"]["v01_c03"]["entries"]
    assert [e["text"] for e in entries] == [
        "можно один прожектор убрать",
        "идея хорошая, фон слишком яркий",
    ]
    for e in entries:
        assert e["prompt_text"] in prompt and e["text"] not in prompt
    assert [e["attempt"] for e in entries] == [1, 2]


# ---------------------------------------------------------------- item 4: normalization


def test_human_fix_is_normalized_through_the_host_agent(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="host")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    decisions = {cid: "accept" for cid in service.status(ws, rid)["waiting"]}
    decisions["v02_c01"] = "fix: камера сверху обрезается, опустить ниже"
    service.decide_gate(ws, rid, "g2", decisions, reviewer="panin")
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    runtime.llm = HostAgentProvider()
    result = service.advance(ws, rid, runtime)
    assert result["state"] == "awaiting_agent"
    assert result["pending_tasks"] == ["tasks/fixnorm.v02_c01.human-1.task.json"]
    assert not store.exists("cards/v02/v02_c01/attempt_02/bundle.json")  # nothing rendered yet
    task = store.read_json(result["pending_tasks"][0])
    assert task["step"] == "fix_normalize" and task["comment"].startswith("камера")
    answers = {
        "fix_normalize": {
            "constraint": "Lower the camera so its top stays inside the frame with a clear margin.",
            "codes": ["crop"],
        },  # fmt: skip
    }
    for _ in range(4):
        if result["state"] != "awaiting_agent":
            break
        for rel in result["pending_tasks"]:
            task = store.read_json(rel)
            data = answers.get(task["step"]) or {
                "answers": {q["rule_id"]: {"answer": "no", "evidence": "ok"}
                            for q in task["questions"]}
            }  # fmt: skip
            store.write(rel.replace(".task.json", ".answer.json"),
                        {**data, "task_sha256": task["task_sha256"], "provider": "claude-code",
                         "model": "host"})  # fmt: skip
        result = service.advance(ws, rid, runtime)
    assert result["state"] == "awaiting_g2", result
    prompt = bundle(ws, rid, "v02_c01", 2).prompt_text
    assert "Lower the camera so its top stays inside the frame" in prompt
    assert "камера" not in prompt
    entry = store.manifest().counters["fix"]["v02_c01"]["entries"][0]
    assert entry["text"] == "камера сверху обрезается, опустить ниже"
    assert entry["normalized_by"] == "claude-code:host" and entry["inferred_codes"] == ["crop"]
    norm = [e for e in store.events() if e.type == "fix_recorded"
            and e.payload["kind"] == "normalized"]  # fmt: skip
    assert norm[0].payload["original"] == entry["text"]
    # The inferred code was verified on attempt 2 through the inspector task.
    assert evaluation(ws, rid, "v02_c01", 2).fix_checks["crop"]["resolved"] is True


def test_bad_normalizer_answer_falls_back_to_the_original(library):
    with pytest.raises(ValueError, match="English"):
        fixes.check_normalized({"constraint": "убрать прожектор", "codes": []}, library)
    with pytest.raises(ValueError, match="unknown"):
        fixes.check_normalized({"constraint": "Remove one spotlight.", "codes": ["x"]}, library)
    assert fixes.check_normalized({"constraint": " Remove one spotlight. ",
                                   "codes": ["props", "other"]}, library) == (
        "Remove one spotlight.", ["props"])  # fmt: skip


# ---------------------------------------------------------------- item 5: verification


def test_unresolved_fix_is_fixed_again_then_rejected_at_the_limit(ws):
    rid = new_run(
        ws,
        fake_mask={"crop": {"v01_c02": [1]}},
        fake_llm={"unresolved_fixes": {"v01_c02": [2, 3]}},
    )
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    card = store.manifest().cards["v01_c02"]
    second = evaluation(ws, rid, "v01_c02", 2)
    assert second.fix_checks["crop"] == {
        "answer": "yes",
        "evidence": "fake inspector: still visible",
        "resolved": False,
        "fix_requested_for_attempt": 1,
    }
    assert second.decision == "fix" and "fix_check:crop:unresolved" in second.reasons
    assert second.judge is None
    # Two fix rounds (max_fix_rounds = 2) are used up: the third failure rejects the card.
    assert card.state == "rejected" and card.attempts == 3 and card.reason == "max_fix_rounds"
    checks = [e.payload for e in store.events()
              if e.type == "check_result" and e.payload["check"] == "fix:crop"]  # fmt: skip
    assert [c["passed"] for c in checks] == [False, False]


def test_crop_fix_question_asks_about_the_top_only(library):
    """User 2026-11-09 («Да, все три», п. 3): `crop` flags only the top; the fix check must not
    ask about the bottom or side edges (shadow-03 comedy v02_c04 was rejected that way)."""
    question = library.question("crop")
    assert "top" in question
    assert "bottom" not in question and "side" not in question


def test_unresolved_crop_fix_goes_to_g2_at_the_limit(ws):
    """With a person at G2, a crop fix still unresolved after the last round is shown to the
    person (flag), not rejected; without G2 it is still rejected (previous test)."""
    rid = new_run(
        ws,
        brief(hitl=Hitl(g1="none", g2="flagged")),
        fake_mask={"crop": {"v01_c02": [1]}},
        fake_llm={"unresolved_fixes": {"v01_c02": [2, 3]}},
        run_id="cfx",
    )
    result = service.advance(ws, rid)
    card = RunStore(ws.run_dir(rid)).manifest().cards["v01_c02"]
    assert evaluation(ws, rid, "v01_c02", 3).reasons == ["fix_check:crop:unresolved"]
    assert card.state == "flagged" and card.attempts == 3
    assert card.reason == "crop_limit: fix_check:crop:unresolved"
    assert result["state"] == "awaiting_g2" and "v01_c02" in result["waiting"]


def test_unresolved_fix_retried_once_then_accepted(ws):
    rid = new_run(
        ws,
        fake_mask={"crop": {"v01_c02": [1]}},
        fake_llm={"unresolved_fixes": {"v01_c02": [2]}},
    )
    assert service.advance(ws, rid)["state"] == "completed"
    card = RunStore(ws.run_dir(rid)).manifest().cards["v01_c02"]
    assert card.state == "accepted" and card.accepted_attempt == 3
    record = RunStore(ws.run_dir(rid)).manifest().counters["fix"]["v01_c02"]
    assert len(record["entries"]) == 1 and record["entries"][0]["attempts"] == [1, 2]


def test_uncertain_fix_check_goes_to_review(ws):
    checked = card_qa.fix_checks({}, {"props": 1})
    assert checked["props"]["resolved"] is None
    hard = card_qa.HardResult({"format": {"passed": True}})
    profile = card_qa.JudgeProfile(id="fake", provider="fake")
    judge = card_qa.JudgeOutcome(judge_model="fake:x", decision="accept",
                                 decision_policy="definite-failure-first-v1")  # fmt: skip
    decided = card_qa.decide(hard, [], judge, profile, {}, checked)
    assert decided.decision == "flag" and decided.reasons == ["fix_check:props:uncertain"]


# ---------------------------------------------------------------- item 6: blind G2


def _blind_page(ws, rid):
    page = write_review_page(RunStore(ws.run_dir(rid)), root=ROOT).read_text()
    return page, json.loads(page[page.index("const DATA = ") + 13 : page.index(";\nconst KEY")])


def test_blind_g2_page_hides_models_and_imports_codes(ws, library):
    request = brief(hitl=Hitl(g1="none", g2="flagged"))
    rid = service.create_run(
        ws, request, llm="fake", render=["fake", "fake-b"], judge="none", shadow_judges=[],
        run_id="blindrun", compare_models=True,
    )  # fmt: skip
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    page, data = _blind_page(ws, rid)
    for secret in ("fake-b", "fake-backend", "blindrun", "v01m2", "v01_c", "judge:"):
        assert secret not in page, secret
    assert data["blind"] is True and data["run_id"] is None
    script = ws.run_dir(rid) / "page.js"
    script.write_text(page.split("<script>")[1].split("</script>")[0])
    if shutil.which("node"):
        assert subprocess.run(["node", "--check", str(script)]).returncode == 0
    assert {r["variant_id"] for r in data["rows"]} == {"Ряд 1", "Ряд 2"}
    assert set(data["reasons"]) == set(library.codes)
    cells = [c for r in data["rows"] for c in r["cells"]]
    assert sorted(c["id"] for c in cells) == [f"card-{n:02d}" for n in range(1, 21)]
    assert all(c["attempts"] is None and "card_id" not in c for c in cells)
    store = RunStore(ws.run_dir(rid))
    mapping = store.read_json(BLIND_MAP)["items"]
    assert _blind_page(ws, rid)[1]["page_id"] == data["page_id"]  # stable on rewrite
    ids = sorted(mapping, key=lambda k: mapping[k])
    export = {
        "format": "cardforge-g2-review-v2", "gate": "g2", "blind": True,
        "page_id": data["page_id"], "reviewer": "Оксана",
        "decisions": {k: "accept" for k in ids},
        "notes": {}, "reasons": {},
    }  # fmt: skip
    crop_id = next(k for k in ids if mapping[k] == "v01_c02")
    note_id = next(k for k in ids if mapping[k] == "v01m2_c04")
    export["decisions"][crop_id] = "fix"  # codes alone
    export["reasons"][crop_id] = ["crop", "other"]
    export["decisions"][note_id] = "reject"
    export["reasons"][note_id] = ["style"]
    export["notes"][note_id] = "не наш стиль"
    path = ws.run_dir(rid) / "g2-blind.json"
    path.write_text(json.dumps(export, ensure_ascii=False))
    cli = ["review", rid, "--gate", "g2", "--from-file", str(path), "--runs-dir",
           str(ws.runs_dir), "--root", str(ROOT)]  # fmt: skip
    result = CliRunner().invoke(app, cli)
    assert result.exit_code in (0, 2), result.output
    gate = store.read_json("review/g2.json")
    assert gate["decisions"]["v01_c02"]["reasons"] == ["crop", "other"]
    event = next(e for e in store.events()
                 if e.type == "human_decision" and e.card_id == "v01_c02")  # fmt: skip
    assert event.payload["blind"] is True and event.payload["reasons"] == ["crop", "other"]
    # Labels: the blind-package rater shape, read the way stage04_blind_targets.py reads it.
    labels = store.read_json(g2_labels.path_for("Оксана"))
    assert labels["format"] == g2_labels.FORMAT and labels["reviewer"] == "Оксана"
    rating = labels["ratings"]["blindrun/v01m2_c04.a01"]
    assert rating["decision"] == "reject" and rating["reasons"] == ["style"]
    assert rating["note"] == "не наш стиль" and rating["blind"] is True
    assert len(rating["image_sha256"]) == 64
    spec_ = importlib.util.spec_from_file_location(
        "targets", ROOT / "scripts/stage04_blind_targets.py"
    )
    targets = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(targets)
    as_rater = {i: r["decision"] for i, r in labels["ratings"].items()}
    lenient = targets.binary(as_rater, targets.PRIMARY_MODE)
    assert lenient["blindrun/v01_c02.a01"] is True and lenient["blindrun/v01m2_c04.a01"] is False
    assert len(lenient) == 20
    # The codes-only fix reached the next prompt; the "other" box adds nothing.
    service.advance(ws, rid)
    prompt = bundle(ws, rid, "v01_c02", 2).prompt_text
    assert "Correction for this attempt: " + library.codes["crop"].prompt in prompt
    # A file from another page is refused.
    export["page_id"] = "000000000000"
    path.write_text(json.dumps(export, ensure_ascii=False))
    assert CliRunner().invoke(app, cli).exit_code == 1


def test_decide_gate_reason_rules_keep_old_callers(ws):
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="rules")
    service.advance(ws, rid)
    with pytest.raises(service.ServiceError, match="fix needs an instruction"):
        service.decide_gate(ws, rid, "g2", {"v01_c01": "fix"}, reviewer="p")
    with pytest.raises(service.ServiceError, match="fix needs an instruction"):
        service.decide_gate(ws, rid, "g2", {"v01_c01": "fix"}, reviewer="p",
                            reasons={"v01_c01": ["other"]})  # fmt: skip
    with pytest.raises(service.ServiceError, match="unknown reason"):
        service.decide_gate(ws, rid, "g2", {"v01_c01": "reject"}, reviewer="p",
                            reasons={"v01_c01": ["oops"]})  # fmt: skip
    service.decide_gate(ws, rid, "g2", {"v01_c01": "fix"}, reviewer="p",
                        reasons={"v01_c01": ["props"]})  # fmt: skip
    service.decide_gate(ws, rid, "g2", {"v01_c02": "fix: проще"}, reviewer="p")  # old form
    gate = RunStore(ws.run_dir(rid)).read_json("review/g2.json")
    assert gate["decisions"]["v01_c02"]["text"] == "fix: проще"
    assert "reasons" not in gate["decisions"]["v01_c02"]


# ---------------------------------------------------------------- item 7: seed honesty


def test_profiles_without_seed_record_a_new_image(ws, monkeypatch):
    original = Workspace.config

    def patched(self):
        cfg = original(self)
        cfg["render_profiles"]["fake"]["send_seed"] = False
        return cfg

    monkeypatch.setattr(Workspace, "config", patched)
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="noseed")
    service.advance(ws, rid)
    decisions = {cid: "accept" for cid in service.status(ws, rid)["waiting"]}
    decisions["v01_c05"] = "fix: проще"
    service.decide_gate(ws, rid, "g2", decisions, reviewer="p")
    service.advance(ws, rid)
    store = RunStore(ws.run_dir(rid))
    record = store.manifest().counters["fix"]["v01_c05"]
    assert record["same_seed"] is False and record["retry"] == "new_image"
    assert record["seed_sent"] is False
    event = next(e for e in store.events() if e.type == "fix_recorded")
    assert event.payload["retry"] == "new_image"
    assert bundle(ws, rid, "v01_c05", 2).params.seed != bundle(ws, rid, "v01_c05", 1).params.seed


# ---------------------------------------------------------------- phase 3: description


def test_human_fix_rewrites_the_object_description(ws):
    """A G2 fix that removes a part also rewrites `object_description` (phase 3,
    2026-11-04): the next prompt no longer asks for what the fix removes."""
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), judge="none", run_id="desc")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    decisions = {cid: "accept" for cid in service.status(ws, rid)["waiting"]}
    decisions["v02_c01"] = "fix: без будки"
    service.decide_gate(ws, rid, "g2", decisions, reviewer="panin")
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    runtime.llm = HostAgentProvider()
    result = service.advance(ws, rid, runtime)
    task = store.read_json(result["pending_tasks"][0])
    assert task["schema_version"] == "fix_normalize.v2" and task["object_description"]
    old_description = task["object_description"]
    answers = {
        "fix_normalize": {"constraint": "Show the object alone, with no booth next to it.",
                          "codes": ["props"],
                          "object_description": "plain test object without any booth"},
    }  # fmt: skip
    for _ in range(4):
        if result["state"] != "awaiting_agent":
            break
        for rel in result["pending_tasks"]:
            t = store.read_json(rel)
            data = answers.get(t["step"]) or {
                "answers": {q["rule_id"]: {"answer": "no", "evidence": "ok"}
                            for q in t["questions"]}
            }  # fmt: skip
            store.write(rel.replace(".task.json", ".answer.json"),
                        {**data, "task_sha256": t["task_sha256"], "provider": "claude-code",
                         "model": "host"})  # fmt: skip
        result = service.advance(ws, rid, runtime)
    # The fake template names objects by `object_name`: check the spec every later step of
    # the card uses (render bundle, inspector, judges) instead of the fake prompt text.
    from types import SimpleNamespace

    from cardforge.contracts.request import CardSpec

    plans = store.read_json("plan/variants.json")
    spec = next(CardSpec.model_validate(c) for p in plans for c in p["cards"]
                if c["card_id"] == "v02_c01")  # fmt: skip
    ctx = SimpleNamespace(manifest=store.manifest())
    assert service._effective_spec(ctx, spec).object_description == (
        "plain test object without any booth"
    )
    assert service._effective_spec(ctx, spec).object_name == spec.object_name
    assert spec.object_description == old_description or not spec.object_description
    entry = store.manifest().counters["fix"]["v02_c01"]["entries"][0]
    assert entry["object_description"] == "plain test object without any booth"


def test_description_answer_is_validated():
    assert fixes.check_description({"object_description": "  "}) is None
    assert fixes.check_description({}) is None
    assert fixes.check_description({"object_description": " a  hat "}) == "a hat"
    with pytest.raises(ValueError, match="English"):
        fixes.check_description({"object_description": "шляпа"})
    history = [{"object_description": "a"}, {"text": "x"}, {"object_description": "b"}]
    assert fixes.current_description(history) == "b"
    assert fixes.current_description([{"text": "x"}]) is None


def test_crop_only_card_goes_to_g2_at_the_limit(ws):
    """User 2026-11-04 («Да, на G2 человеку»): a card stopped by the crop check alone is shown
    to the human at G2 after the last attempt instead of a silent rejection; without a human
    at G2 it is still rejected."""
    crop = {"crop": {"v01_c02": [1, 2, 3]}}
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), fake_mask=crop, run_id="cl")
    result = service.advance(ws, rid)
    card = RunStore(ws.run_dir(rid)).manifest().cards["v01_c02"]
    assert evaluation(ws, rid, "v01_c02", 3).reasons == ["hard_check:crop"]
    assert card.state == "flagged" and card.attempts == 3
    assert card.reason == "crop_limit: hard_check:crop"
    assert result["state"] == "awaiting_g2" and "v01_c02" in result["waiting"]
    rid2 = new_run(ws, fake_mask=crop, run_id="cl2")
    service.advance(ws, rid2)
    card2 = RunStore(ws.run_dir(rid2)).manifest().cards["v01_c02"]
    assert card2.state == "rejected" and card2.reason == "max_fix_rounds"


@pytest.mark.parametrize(
    ("reasons", "state"),
    [
        (["hard_check:safe_zone"], "flagged"),
        (["hard_check:fill"], "flagged"),
        (["hard_check:crop", "hard_check:safe_zone"], "flagged"),
        (["hard_check:background_simplicity"], "rejected"),
        (["hard_check:safe_zone", "hard_check:background_simplicity"], "rejected"),
    ],
)
def test_geometry_only_card_goes_to_g2_at_the_limit(ws, monkeypatch, reasons, state):
    """2026-11-05: the crop-limit rule covers the geometric mask checks (safe zone, frame
    fill); background simplicity is not geometric and is still rejected at the limit."""
    original = service._apply_decision

    def rewrite(ctx, card_id, evaluation):
        if card_id == "v01_c02" and evaluation.reasons == ["hard_check:crop"]:
            evaluation = evaluation.model_copy(update={"reasons": list(reasons)})
        return original(ctx, card_id, evaluation)

    monkeypatch.setattr(service, "_apply_decision", rewrite)
    crop = {"crop": {"v01_c02": [1, 2, 3]}}
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), fake_mask=crop, run_id="gl")
    service.advance(ws, rid)
    card = RunStore(ws.run_dir(rid)).manifest().cards["v01_c02"]
    assert card.state == state and card.attempts == 3
    expected = "crop_limit: " + ", ".join(reasons) if state == "flagged" else "max_fix_rounds"
    assert card.reason == expected


# ---------------------------------------------------------------- card_reopen (2026-11-09)


def test_card_reopen_brings_a_rejected_card_back_to_g2(ws):
    """User 2026-11-09 («реализуй»): a card rejected at G2 (or by the core at a limit) comes
    back to G2 with its last image, without a new render call, and can be accepted."""
    crop = {"crop": {"v01_c02": [1, 2, 3]}}
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), fake_mask=crop, run_id="cr")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    service.decide_gate(ws, rid, "g2", {"v01_c02": "reject"}, "R1")
    assert service.advance(ws, rid)["state"] == "partial"
    store = RunStore(ws.run_dir(rid))
    renders = sum(e.type == "render_started" for e in store.events())
    with pytest.raises(service.ServiceError, match="reason"):
        service.card_reopen(ws, rid, "v01_c02", " ", "R1")
    result = service.card_reopen(ws, rid, "v01_c02", "по новому правилу crop", "R1")
    card = store.manifest().cards["v01_c02"]
    assert result["state"] == "rendering" and card.state == "flagged"
    assert card.accepted_attempt is None and card.attempts == 3
    gate = store.read_json("review/g2.json")
    assert "v01_c02" not in gate["decisions"]
    assert gate["history"][-1]["superseded"] == "card_reopen: по новому правилу crop"
    reopened = [e.payload for e in store.events() if e.type == "card_reopened"]
    assert reopened == [{"from": "rejected", "attempt_no": 3,
                         "reason": "по новому правилу crop", "reviewer": "R1"}]  # fmt: skip
    waiting = service.advance(ws, rid)
    assert waiting["state"] == "awaiting_g2" and waiting["waiting"] == ["v01_c02"]
    item = next(i for i in store.read_json("review/g2.json")["items"]
                if i["card_id"] == "v01_c02")  # fmt: skip
    assert item["image"].endswith("attempt_03/final.png")
    service.decide_gate(ws, rid, "g2", {"v01_c02": "accept"}, "R1")
    assert service.advance(ws, rid)["state"] == "completed"
    card = store.manifest().cards["v01_c02"]
    assert card.state == "accepted" and card.accepted_attempt == 3
    assert sum(e.type == "render_started" for e in store.events()) == renders


def test_card_reopen_of_an_accepted_card_resets_ip_check(ws):
    from cardforge.policy import ip_check

    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="all")), run_id="ca")
    assert service.advance(ws, rid)["state"] == "awaiting_g2"
    store = RunStore(ws.run_dir(rid))
    cards = list(store.manifest().cards)
    service.decide_gate(ws, rid, "g2", {c: "accept" for c in cards}, "R1")
    assert service.advance(ws, rid)["state"] == "completed"
    ip_check.record_human_decision(store, "v01", confirmed=True, human_answer="да", reviewer="R1")
    service.card_reopen(ws, rid, "v01_c01", "посмотреть ещё раз", "R1")
    m = store.manifest()
    assert m.ip_check["v01"].status == "pending" and m.cards["v01_c01"].state == "flagged"
    assert [e.type for e in store.events()].count("ip_check_reset") == 1
    assert service.advance(ws, rid)["waiting"] == ["v01_c01"]
    with pytest.raises(service.ServiceError, match="only rejected or accepted"):
        service.card_reopen(ws, rid, "v01_c01", "again", "R1")


def test_card_reopen_cli(ws):
    crop = {"crop": {"v01_c02": [1, 2, 3]}}
    rid = new_run(ws, brief(hitl=Hitl(g1="none", g2="flagged")), fake_mask=crop, run_id="cc")
    service.advance(ws, rid)
    service.decide_gate(ws, rid, "g2", {"v01_c02": "reject"}, "R1")
    service.advance(ws, rid)
    out = CliRunner().invoke(app, ["card-reopen", rid, "--card", "v01_c02", "--reason", "r",
                                   "--reviewer", "R1", "--runs-dir", str(ws.runs_dir)])  # fmt: skip
    assert out.exit_code == 0, out.output
    assert RunStore(ws.run_dir(rid)).manifest().cards["v01_c02"].state == "flagged"
