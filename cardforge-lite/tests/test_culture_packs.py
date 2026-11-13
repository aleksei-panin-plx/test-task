# ruff: noqa: E501
"""Light (user 2026-11-13): every `policies/culture/*.md` pack is active, every plan rule (block,
review, advise) reaches the planner, an object that matches one is replaced and the replacement is
written to policy_notes and shown at G1."""

from __future__ import annotations

import csv
import io
import json

from cardforge.contracts.request import CollectionRequest, Hitl, PlanDraft
from cardforge.llm.fake import FakeLLM, fake_plan
from cardforge.orchestrator import run
from cardforge.orchestrator.workspace import Workspace
from cardforge.planner import planner
from cardforge.policy.actions import REPLACE, action_for
from cardforge.policy.loader import lint, resolve_active

from .conftest import ROOT

NEW_PACK = """---
policy_id: culture.test
policy_kind: culture
region: XX
policy_version: 0.0.1
includes: []
verified: 2026-11-13
review_after: 2027-11-13
---

# Тестовый рынок

```yaml policy
rules:
  - id: XX-OBJ-01
    title: "Лейка как объект"
    severity: review
    stages: [plan]
    applies_to: [object]
    match:
      keywords: ["лейка", "watering can"]
      semantic: "Садовая лейка любого вида"
    alternatives: ["садовый шланг"]
    rationale: "Тестовое ограничение."
  - id: XX-OBJ-02
    title: "Синие вёдра"
    severity: advise
    stages: [plan]
    applies_to: [object]
    match:
      keywords: ["синее ведро"]
      semantic: "Ведро синего цвета"
    alternatives: ["красное ведро"]
    rationale: "Тестовое ограничение."
```
"""


def _ids(packs) -> set[str]:
    return {p.policy_id for p in packs}


def _task(root, markets=()):
    request = CollectionRequest(request_id="t", collection_theme="Лето", set_theme="",
                                producer_wishes_raw="", sets_total=1, markets=list(markets))  # fmt: skip

    class Ctx:
        packs = resolve_active(root, request.markets, [])

    Ctx.root = root
    return planner.plan_task(Ctx(), request)


def test_every_culture_pack_is_active_without_markets():
    assert _ids(resolve_active(ROOT, [], [])) == {"ip.baseline", "culture.cn"}


def test_a_new_culture_file_is_picked_up_and_its_rules_reach_the_planner(root_copy):
    (root_copy / "policies/culture/test.md").write_text(NEW_PACK, encoding="utf-8")
    assert lint(root_copy)["ok"], lint(root_copy)["issues"]
    assert _ids(resolve_active(root_copy, [], [])) == {"ip.baseline", "culture.cn", "culture.test"}
    rules = {r["rule_id"]: r for r in _task(root_copy)["plan_rules"]}
    assert rules["XX-OBJ-01"]["severity"] == "review" and rules["XX-OBJ-02"]["severity"] == "advise"
    assert rules["XX-OBJ-01"]["pack"] == "culture.test" and rules["XX-OBJ-01"]["keywords"] == ["лейка", "watering can"]
    assert rules["XX-OBJ-01"]["alternatives"] == ["садовый шланг"]


def test_all_plan_rules_of_china_reach_the_planner_including_review_and_advise():
    rules = {r["rule_id"]: r for r in _task(ROOT)["plan_rules"]}
    packs = resolve_active(ROOT, [], [])
    want = {r.id for p in packs for r in p.rules if "plan" in r.stages}
    assert set(rules) == want
    assert {rules[i]["severity"] for i in want if i.startswith("CN-")} == {"block", "review", "advise"}


def test_only_restrictions_reach_the_planner():
    """No «what is welcome» hints: the prose of a pack (e.g. genre hints) never enters the task."""
    text = json.dumps(_task(ROOT), ensure_ascii=False)
    assert "Жанровые подсказки" not in text
    china = (ROOT / "policies/culture/china.md").read_text(encoding="utf-8")
    hints = china.split("## Жанровые подсказки", 1)[1].split("\n## ", 1)[0]
    lines = [x.strip("-* ").strip() for x in hints.splitlines() if len(x.strip()) > 30]
    assert lines and not [x for x in lines if x in text]


def test_every_severity_is_replaced_at_plan_stage():
    hitl = Hitl(g1="all", g2="none")
    for severity in ("block", "review", "advise"):
        for verdict in ("violation", "uncertain"):
            assert action_for(severity, verdict, "plan", hitl) == REPLACE


def test_replacement_must_be_written_to_policy_notes():
    draft = PlanDraft.model_validate({**fake_plan(0, "Сад"), "variant_id": "v01"})
    replaced = [{"card_id": "v01_c01", "variant_id": "v01", "rule_id": "CN-POL-01", "object_before": "Глобус"}]
    assert planner.replacement_errors(replaced, [draft])
    noted = draft.model_copy(update={"policy_notes": ["CN-POL-01: «Глобус» → «Компас»: география"]})
    assert not planner.replacement_errors(replaced, [noted])


def test_dry_run_replaces_forbidden_objects_and_shows_it_at_g1(root_copy, tmp_path):
    """Fake LLM plants «Глобус» (CN-POL-01, block), «Лейка» (review) and «Синее ведро» (advise of the
    new pack); the screen sends them back, the repair replaces them, G1 shows packs and replacements."""
    (root_copy / "policies/culture/test.md").write_text(NEW_PACK, encoding="utf-8")
    ws = Workspace(root=root_copy, runs_dir=tmp_path / "runs")
    rid = run.create(ws, "Лето", sets=2, llm="fake")
    out = run.advance(ws, rid, FakeLLM(inject={"v01_c01": "Глобус", "v01_c05": "Лейка", "v02_c01": "Синее ведро"}))
    assert out["state"] == "awaiting_g1", out.get("unmet_rules")
    view = run.plan_view(ws, rid)
    doc = json.loads(open(view["files"]["json"], encoding="utf-8").read())
    objects = {c["card_id"]: c["object_name"] for s in doc["sets"] for c in s["cards"] if c["slot"] == "object"}
    assert not {"Глобус", "Лейка", "Синее ведро"} & set(objects.values())
    reps = {(r["card_id"], r["rule_id"]): r for r in doc["policies"]["replacements"]}
    assert set(reps) == {("v01_c01", "CN-POL-01"), ("v01_c05", "XX-OBJ-01"), ("v02_c01", "XX-OBJ-02")}
    assert reps[("v01_c01", "CN-POL-01")]["object_after"].startswith("Компас")
    assert all(r["note"] for r in reps.values())
    notes = {s["variant_id"]: " ".join(s["policy_notes"]) for s in doc["sets"]}
    assert "CN-POL-01" in notes["v01"] and "XX-OBJ-01" in notes["v01"] and "XX-OBJ-02" in notes["v02"]
    assert {p["id"] for p in doc["policies"]["packs"]} == {"ip.baseline", "culture.cn", "culture.test"}
    md = view["markdown"]
    assert "## Политики" in md and "policies/culture/test.md" in md
    assert "«Глобус» → «Компас" in md and "(XX-OBJ-02, advise)" in md
    rows = list(csv.DictReader(io.StringIO(open(view["files"]["csv"], encoding="utf-8-sig").read())))
    assert next(r for r in rows if r["card_id"] == "v01_c01")["replaced"] == "Глобус (CN-POL-01)"


def test_clean_plan_says_no_replacements(ws):
    rid = run.create(ws, "Лето", sets=1, llm="fake")
    run.advance(ws, rid)
    assert "Замен по ограничениям не понадобилось." in run.plan_view(ws, rid)["markdown"]
