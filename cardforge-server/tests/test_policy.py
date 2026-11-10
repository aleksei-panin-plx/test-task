import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cardforge.cli import app
from cardforge.contracts.policy import PolicyVerdict
from cardforge.contracts.request import CardDraft, CollectionRequest, Hitl, PlanDraft
from cardforge.contracts.run import IpCheck
from cardforge.orchestrator.store import ManifestError, RunStore
from cardforge.policy import ip_check, service
from cardforge.policy.actions import action_for
from cardforge.policy.inspector import qa_rules, run_detectors, verdicts_from_answers
from cardforge.policy.loader import BASELINE, lint, parse_pack, resolve_active
from cardforge.policy.matching import find_terms
from cardforge.policy.prompt_guard import (
    PolicyPromptError,
    applicable_rules,
    constraint_lines,
    guard,
)
from cardforge.policy.report import PENDING_WARNING
from cardforge.policy.screener import (
    conflicts_from,
    plan_errors,
    screen_plan,
    screen_request,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "tests/snapshots/prompt_guard"


def request(wishes="", markets=(), **kw) -> CollectionRequest:
    return CollectionRequest(
        request_id="t",
        collection_theme="Кино",
        set_theme=kw.pop("set_theme", "Хоррор"),
        producer_wishes_raw=wishes,
        markets=list(markets),
        **kw,
    )


def card(name: str, category: int = 1, **kw) -> CardDraft:
    base = {
        "object_name": name,
        "theme_rationale": "genre symbol",
        "category": category,
        "scale_class": "handheld",
        "object_type": "decor",
        "composition": "centered three-quarter view",
    }
    if category == 1:
        base["background"] = {"motif": "soft rays", "color": "teal"}
    if category in (2, 3):
        base["surface"] = "wooden table"
    if category == 4:
        base["environment"] = "old attic"
    return CardDraft(**(base | kw))


def plan(variant="v01", names=None, notes=()) -> PlanDraft:
    names = names or [f"prop {i}" for i in range(10)]
    cards = [card(n, category=(i % 4) + 1) for i, n in enumerate(names)]
    return PlanDraft(
        variant_id=variant, title="t", concept="c", cards=cards, policy_notes=list(notes)
    )


def packs(*markets):
    return resolve_active(ROOT, list(markets), [])


# --- loader and linter -------------------------------------------------------------------


def test_lint_passes_on_both_packs():
    report = lint(ROOT)
    assert report["ok"], report["issues"]
    assert {p["policy_id"] for p in report["packs"]} >= {"ip.baseline", "culture.cn"}


def test_cli_lint_exit_code():
    result = CliRunner().invoke(app, ["policy", "lint", "--root", str(ROOT)])
    assert result.exit_code == 0, result.output


PACK = """---
policy_id: topic.test
policy_kind: topic
policy_version: 0.0.1
review_after: 2030-01-01
---
```yaml policy
rules:
{rules}
```
"""


@pytest.mark.parametrize(
    ("rule", "issue"),
    [
        (
            "  - {id: T-1, title: t, severity: advise, stages: [qa], applies_to: [object],"
            " match: {semantic: s}, rationale: r}",
            "needs qa_question",
        ),
        (
            "  - {id: T-1, title: t, severity: advise, stages: [prompt], applies_to: [object],"
            " match: {semantic: s}, rationale: r}",
            "needs prompt_constraint",
        ),
        (
            "  - {id: T-1, title: t, severity: block, stages: [plan], applies_to: [object],"
            " match: {semantic: s}, rationale: r}",
            "needs alternatives",
        ),
        (
            "  - {id: T-1, title: t, severity: advise, stages: [plan], applies_to: [object],"
            " match: {semantic: s}, rationale: r}\n"
            "  - {id: T-1, title: t, severity: advise, stages: [plan], applies_to: [object],"
            " match: {semantic: s}, rationale: r}",
            "duplicate id",
        ),
        (
            "  - {id: T-1, title: t, severity: advise, stages: [prompt], applies_to: [object],"
            " match: {keywords: [skull], semantic: s}, prompt_constraint: 'no skull',"
            " rationale: r}",
            "names 'skull'",
        ),
    ],
)
def test_lint_reports_rule_problems(tmp_path, rule, issue):
    path = tmp_path / "t.md"
    path.write_text(PACK.format(rules=rule))
    report = lint(tmp_path, [path])
    assert not report["ok"]
    assert any(issue in i for i in report["issues"]), report["issues"]


@pytest.mark.parametrize(
    "body",
    [
        "```yaml policy\nrules: []\n```\n```yaml policy\nrules: []\n```\n",
        "```yaml policy\nrules: []\nextra: 1\n```\n",
    ],
)
def test_lint_rejects_malformed_blocks(tmp_path, body):
    path = tmp_path / "t.md"
    path.write_text("---\npolicy_id: x\npolicy_kind: topic\npolicy_version: 1\n---\n" + body)
    assert not lint(tmp_path, [path])["ok"]


def test_unknown_severity_and_stage_are_rejected(tmp_path):
    rule = (
        "  - {id: T-1, title: t, severity: forbid, stages: [render], applies_to: [object],"
        " match: {semantic: s}, rationale: r}"
    )
    path = tmp_path / "t.md"
    path.write_text(PACK.format(rules=rule))
    assert not lint(tmp_path, [path])["ok"]


# --- block rules cannot be disabled ------------------------------------------------------


def test_baseline_is_always_active_and_first():
    assert [p.policy_id for p in packs()] == [BASELINE]
    assert [p.policy_id for p in packs("cn")] == [BASELINE, "culture.cn"]
    by_path = resolve_active(ROOT, [], ["culture/china"])
    assert [p.policy_id for p in by_path] == [BASELINE, "culture.cn"]


def test_request_has_no_switch_to_disable_rules():
    for field in ("disabled_rules", "waive_rules", "skip_policies", "policy_overrides"):
        with pytest.raises(ValidationError):
            CollectionRequest.model_validate(request().model_dump() | {field: ["CN-VIO-02"]})


@pytest.mark.parametrize("g", ["all", "flagged", "none"])
@pytest.mark.parametrize("review_policy", ["replace", "accept_with_note"])
@pytest.mark.parametrize("verdict", ["violation", "uncertain"])
def test_block_action_ignores_gate_mode_and_review_policy(g, review_policy, verdict):
    hitl = Hitl(g1=g, g2=g, review_policy=review_policy)
    assert action_for("block", verdict, "plan", hitl) == "replace"
    assert action_for("block", verdict, "qa", hitl) == "fix_or_regenerate"


def test_review_actions_follow_matrix():
    assert action_for("review", "violation", "plan", Hitl(g1="all")) == "flag_g1"
    assert action_for("review", "violation", "plan", Hitl(g1="none")) == "replace"
    assert action_for("review", "uncertain", "qa", Hitl(g2="flagged")) == "flag_g2"
    assert action_for("review", "violation", "qa", Hitl(g2="none")) == "regenerate_with_fix"
    none_note = Hitl(g2="none", review_policy="accept_with_note")
    assert action_for("review", "violation", "qa", none_note) == "accept_with_note"
    assert action_for("advise", "uncertain", "qa", Hitl(g2="none")) == "regenerate_with_fix"
    assert action_for("advise", "violation", "qa", Hitl()) == "note"


def test_block_rule_cannot_be_waived_in_prompt():
    with pytest.raises(PolicyPromptError, match="CN-VIO-02"):
        guard("a skull on a shelf", packs("CN"), waived={"CN-VIO-02"})
    guard("a revolver on a table", packs("CN"), waived={"CN-VIO-04"})  # review: human waiver


def test_plan_with_block_object_fails_even_in_gate_none():
    req = request(markets=["CN"], hitl=Hitl(g1="none", g2="none"))
    p = plan(names=["skull candle"] + [f"prop {i}" for i in range(9)])
    errors = plan_errors(screen_plan([p], req, packs("CN")), [p], req)
    assert any("CN-VIO-02" in e for e in errors)


# --- checklist single source -------------------------------------------------------------

SKIP_DIRS = {
    ".venv", ".uv-cache", ".bootstrap", "runs", "dataset", "archive", "models", ".git", "dist"
}  # fmt: skip
# The roadmap is the specification the checklist was written from, not a runtime instruction.
SPEC_FILES = {"Implementation Roadmap.md"}
# Copies of generated run reports, which must render the checklist from baseline.md.
GENERATED = ("docs/stages/evidence/03/runs/",)


def test_checklist_lives_only_in_baseline():
    baseline = ROOT / "policies/ip/baseline.md"
    items = parse_pack(baseline).checklist
    assert len(items) == 6
    copies = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if (
            not path.is_file()
            or rel.parts[0] in SKIP_DIRS
            or path.suffix not in {".md", ".py", ".toml", ".json", ".yaml", ".txt", ".html"}
            or path == baseline
            or str(rel) in SPEC_FILES
            or str(rel).startswith(GENERATED)
            or "__pycache__" in rel.parts
        ):
            continue
        text = path.read_text(errors="ignore").lower()
        copies += [f"{rel}: {i}" for i in items if i.lower().rstrip(".") in text]
    assert not copies


@pytest.mark.parametrize("rel", ["AGENTS.md", "agents/compliance-reviewer.md"])
def test_agent_instructions_reference_checklist(rel):
    text = (ROOT / rel).read_text()
    assert "policies/ip/baseline.md" in text
    assert "Чек-лист авторских прав" in text


# --- matching and screener ---------------------------------------------------------------


def test_russian_inflections_hit_keywords():
    assert find_terms("побольше скелетов и крови", ["скелет", "кровь"]) == ["скелет", "кровь"]
    assert find_terms("Superheroes like in Marvel", ["marvel"]) == ["marvel"]
    assert find_terms("crossbow and mapping", ["cross", "map"]) == []
    assert find_terms("подзорная труба, атласная лента", ["труп", "атлас"]) == []
    assert find_terms("трупы и атласы", ["труп", "атлас"]) == ["труп", "атлас"]


def test_marvel_brief_records_ip_conflict():
    req = request("супергерои, как в Marvel", set_theme="Супергерои")
    active = packs()
    conflicts = conflicts_from(screen_request(req, active), active, req)
    assert [(c.kind, c.rule_id) for c in conflicts] == [("ip", "IP-BRD-01")]


def test_cn_horror_brief_conflicts_and_baseline_only_without_cn():
    req = request("хоррор, побольше скелетов и крови", markets=["CN"])
    active = packs("CN")
    ids = {c.rule_id for c in conflicts_from(screen_request(req, active), active, req)}
    assert {"CN-VIO-01", "CN-VIO-02", "CN-SUP-02"} <= ids
    plain = request("хоррор, побольше скелетов и крови")
    assert screen_request(plain, packs()) == []


def test_policy_notes_must_explain_conflicts():
    req = request("кровь", markets=["CN"], set_theme="Детектив")
    active = packs("CN")
    req.conflicts = conflicts_from(screen_request(req, active), active, req)
    bare = plan()
    assert plan_errors([], [bare], req) == ["v01: policy_notes must explain CN-VIO-01"]
    noted = plan(notes=["CN-VIO-01: кровь заменена подсвечником"])
    assert plan_errors([], [noted], req) == []


# --- PromptGuard snapshots ---------------------------------------------------------------

PROMPT_CASES = {
    "cn_horror_candlestick": (["CN"], "хоррор, побольше скелетов и крови", "brass candlestick"),
    "superhero_cape": ([], "супергерои, как в Marvel", "red hero cape on a hanger"),
    "plain_western_hat": ([], "побольше ретро", "cowboy hat"),
}


def build_prompt(markets, wishes, obj):
    """Stand-in for the stage-4 template: object block plus policy constraints."""
    active = packs(*markets)
    req = request(wishes, markets=markets)
    verdicts = screen_request(req, active)
    constraints = constraint_lines(applicable_rules(active, verdicts, {"request", "v01_c01"}))
    text = f"{obj}, centered three-quarter view, soft studio light."
    if constraints:
        text += "\nConstraints: " + "; ".join(constraints) + "."
    return text, active


@pytest.mark.parametrize("name", sorted(PROMPT_CASES))
def test_prompt_guard_snapshots(name):
    text, active = build_prompt(*PROMPT_CASES[name])
    guard(text, active)
    snap = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(text + "\n")
    assert text + "\n" == snap.read_text()


@pytest.mark.parametrize(
    ("markets", "term", "rule"),
    [
        (["CN"], "a skull", "CN-VIO-02"),
        (["CN"], "poker chips", "CN-GAM-01"),
        (["CN"], "world map backdrop", "CN-POL-01"),
        ([], "Marvel style cape", "IP-BRD-01"),
        ([], "a lightsaber", "IP-PRP-01"),
        ([], "Spider-Man mask", "IP-CHR-01"),
    ],
)
def test_prompt_guard_rejects_forbidden_terms(markets, term, rule):
    with pytest.raises(PolicyPromptError, match=rule):
        guard(f"{term}, centered, soft light", packs(*markets))


def test_prompt_guard_allows_term_without_its_pack():
    guard("a skull on a shelf", packs())


# --- inspector and detectors -------------------------------------------------------------


def test_inspector_selects_only_applicable_questions():
    fired = [
        PolicyVerdict(
            rule_id="CN-VIO-02",
            stage="plan",
            target="request",
            verdict="violation",
            severity="block",
            evidence="",
            action="replace",
        )
    ]
    ids = {r.id for r in qa_rules(packs("CN"), fired, {"request", "v01_c01"})}
    assert {"IP-QA-01", "CN-TXT-01", "CN-TXT-02", "CN-VIO-02"} <= ids
    assert "CN-GAM-01" not in ids and "CN-CHR-01" not in ids
    assert {r.id for r in qa_rules(packs(), [], {"v01_c01"})} == {"IP-QA-01"}


def test_missing_inspector_answer_is_uncertain_review():
    rules = qa_rules(packs(), [], {"v01_c01"})
    [v] = verdicts_from_answers("v01_c01", rules, {}, Hitl())
    assert (v.verdict, v.action) == ("uncertain", "flag_g2")


def test_ocr_detectors_for_china():
    ocr = {
        "status": "ok",
        "observations": [
            {"text": "停", "confidence": 0.9},
            {"text": "No 4", "confidence": 0.8},
        ],
    }
    verdicts = {v.rule_id: v for v in run_detectors("c", ocr, packs("CN"), Hitl())}
    assert verdicts["CN-TXT-01"].verdict == "violation"
    assert verdicts["CN-TXT-01"].action == "fix_or_regenerate"
    assert verdicts["CN-SYM-02"].verdict == "violation"
    assert verdicts["CN-TXT-02"].verdict == "violation"
    clean = run_detectors("c", {"status": "ok", "observations": []}, packs("CN"), Hitl())
    assert {v.verdict for v in clean} == {"pass"}
    missing = run_detectors("c", {"status": "unavailable"}, packs("CN"), Hitl())
    assert {v.verdict for v in missing} == {"uncertain"}
    assert run_detectors("c", ocr, packs(), Hitl()) == []


# --- run flow, report and ip_check -------------------------------------------------------


def write_plans(run: Path, plans: list[PlanDraft]):
    (run / "tasks/plan.answer.json").write_text(
        json.dumps([p.model_dump(mode="json") for p in plans], ensure_ascii=False)
    )


def write_semantic(run: Path, findings=(), task_sha256=None):
    if task_sha256 is None:
        assert service.screen(ROOT, run)["state"] == "awaiting_agent"
        task_sha256 = json.loads((run / "tasks/policy_screen.task.json").read_text())["task_sha256"]
    payload = {"task_sha256": task_sha256, "provider": "test", "model": "fixture"}
    (run / "tasks/policy_screen.answer.json").write_text(
        json.dumps(payload | {"findings": list(findings)})
    )


def answer(run: Path, plans: list[PlanDraft], findings=()):
    write_plans(run, plans)
    write_semantic(run, findings)


def planned_run(tmp_path, gate="all", markets=(), wishes="побольше ретро"):
    run = tmp_path / "run"
    req = request(wishes, markets=markets, hitl=Hitl(g1=gate, g2=gate))
    started = service.start(ROOT, run, req)
    assert started["state"] == "awaiting_agent"
    notes = [c["rule_id"] + ": замена" for c in started["conflicts"]]
    answer(run, [plan(f"v0{i}", notes=notes) for i in (1, 2, 3)])
    assert service.screen(ROOT, run)["state"] == "planned"
    return run


@pytest.mark.parametrize("gate", ["all", "flagged", "none"])
def test_every_variant_starts_pending_and_report_warns(tmp_path, gate):
    run = planned_run(tmp_path, gate=gate)
    manifest = RunStore(run).manifest()
    assert {v: c.status for v, c in manifest.ip_check.items()} == dict.fromkeys(
        ("v01", "v02", "v03"), "pending"
    )
    report = (run / "report.md").read_text()
    assert "## Авторские права" in report and "## Политики" in report
    assert PENDING_WARNING in report
    for item in parse_pack(ROOT / "policies/ip/baseline.md").checklist:
        assert item in report
    assert not [e for e in RunStore(run).events() if e.type == "human_decision"]


def test_confirmed_without_human_decision_is_rejected(tmp_path):
    store = RunStore(planned_run(tmp_path))
    manifest = store.manifest()
    manifest.ip_check["v01"] = IpCheck(status="confirmed")
    with pytest.raises(ManifestError):
        store.save_manifest(manifest)
    # A hand-edited manifest is caught on the next read as well.
    raw = json.loads(store.path("manifest.json").read_text())
    raw["ip_check"]["v01"] = {"status": "confirmed", "event_id": "forged"}
    store.path("manifest.json").write_text(json.dumps(raw))
    with pytest.raises(ManifestError):
        store.manifest()


def test_non_human_event_cannot_confirm(tmp_path):
    store = RunStore(planned_run(tmp_path))
    event = store.emit(
        "human_decision",
        {"subject": "ip_check", "decision": "confirmed", "decided_by": "auto"},
        variant_id="v01",
    )
    manifest = store.manifest()
    manifest.ip_check["v01"] = IpCheck(status="confirmed", event_id=event.event_id)
    with pytest.raises(ManifestError):
        store.save_manifest(manifest)


def test_human_decision_confirms_one_variant(tmp_path):
    run = planned_run(tmp_path)
    store = RunStore(run)
    with pytest.raises(ValueError):
        ip_check.record_human_decision(
            store, "v01", confirmed=True, human_answer=" ", reviewer="qa"
        )
    check = ip_check.record_human_decision(
        store, "v01", confirmed=True, human_answer="Проверил, подтверждаю", reviewer="qa"
    )
    assert check.status == "confirmed"
    service.report(ROOT, run)
    report = (run / "report.md").read_text()
    assert "v01: `ip_check: confirmed`" in report
    assert "v02: `ip_check: pending`" in report and PENDING_WARNING in report


def test_cli_confirm_needs_human_answer(tmp_path):
    run = planned_run(tmp_path)
    args = ["policy", "confirm-ip", str(run), "--variant", "v01", "--reviewer", "agent"]
    result = CliRunner().invoke(app, [*args, "--root", str(ROOT)], input="")
    assert result.exit_code != 0
    assert RunStore(run).manifest().ip_check["v01"].status == "pending"


def test_screen_waits_for_semantic_answer_and_repairs(tmp_path):
    run = tmp_path / "run"
    req = request("хоррор, побольше скелетов и крови", markets=["CN"])
    started = service.start(ROOT, run, req)
    rule_ids = {c["rule_id"] for c in started["conflicts"]}
    assert {"CN-VIO-01", "CN-VIO-02", "CN-SUP-02"} <= rule_ids
    notes = [f"{r}: замена" for r in sorted(rule_ids)]
    bad = plan("v01", names=["skull"] + [f"prop {i}" for i in range(9)], notes=notes)
    write_plans(run, [bad] + [plan(f"v0{i}", notes=notes) for i in (2, 3)])
    finding = {
        "target": "v02_c03",
        "rule_id": "CN-SUP-01",
        "verdict": "uncertain",
        "evidence": "prop 2",
    }
    write_semantic(run, [finding])
    result = service.screen(ROOT, run)
    assert result["state"] == "planning_repair"
    assert any("v01_c01: CN-VIO-02" in e for e in result["errors"])
    assert any("v02_c03: CN-SUP-01" in e for e in result["errors"])
    assert not (run / "plan/variants.json").exists()
    # Repair: the old semantic answer no longer matches the task and cannot clear the plan.
    write_plans(run, [plan(f"v0{i}", notes=notes) for i in (1, 2, 3)])
    stale = service.screen(ROOT, run)
    assert (stale["state"], stale["reason"]) == ("awaiting_agent", "stale answer")
    write_semantic(run, [], task_sha256=stale["task_sha256"])
    assert service.screen(ROOT, run)["state"] == "planned"
