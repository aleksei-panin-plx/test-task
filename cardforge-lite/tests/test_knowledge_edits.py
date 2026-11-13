# ruff: noqa: E501
"""Knowledge is edited without code: prompts/planner/*.md, vocabulary.yaml, set_grammar/v1.yaml
(docs/guides/customize-knowledge.md)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from cardforge.contracts.request import CollectionRequest
from cardforge.doctor import collect
from cardforge.orchestrator import run
from cardforge.orchestrator.workspace import Workspace
from cardforge.planner import planner
from cardforge.policy.loader import resolve_active

from .conftest import ROOT


def _task(root: Path) -> dict:
    request = CollectionRequest(request_id="t", collection_theme="Лето", set_theme="",
                                producer_wishes_raw="", sets_total=2)  # fmt: skip

    class Ctx:
        packs = resolve_active(root, [], [])

    Ctx.root = root
    return planner.plan_task(Ctx(), request)


def test_instruction_is_the_prompt_files():
    task = _task(ROOT)
    assert task["instruction"].startswith("Plan the whole collection")
    assert "`main_object`" in task["instruction"] and "`environment`" in task["instruction"]
    assert "<!--" not in task["instruction"] and "Инструкция планировщика" not in task["instruction"]
    assert task["collection_layout"]["sets_total"] == 2


def test_prompt_edit_changes_the_task(root_copy):
    path = root_copy / "prompts/planner/card_rules.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nNever plan umbrellas.\n", encoding="utf-8")
    task = _task(root_copy)
    assert task["instruction"].endswith("Never plan umbrellas.")
    assert task["task_sha256"] != _task(ROOT)["task_sha256"]


def test_vocabulary_and_grammar_edits_reach_the_validator(root_copy, tmp_path):
    # A motif removed from category 3 and a new background colour in the vocabulary.
    gpath = root_copy / "knowledge/set_grammar/v1.yaml"
    grammar = yaml.safe_load(gpath.read_text(encoding="utf-8"))
    grammar["category_motifs"]["1"] = ["gradient"]
    gpath.write_text(json.dumps(grammar, ensure_ascii=False, indent=2), encoding="utf-8")
    vpath = root_copy / "knowledge/catalog/vocabulary.yaml"
    vocab = yaml.safe_load(vpath.read_text(encoding="utf-8"))
    vocab["color"]["mint"] = "Мятный фон (пример правки)."
    vpath.write_text(json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8")
    task = _task(root_copy)
    assert "mint" in task["vocabulary"]["color"]
    ws = Workspace(root=root_copy, runs_dir=tmp_path / "runs")
    rid = run.create(ws, "Лето", sets=1, llm="fake")
    out = run.advance(ws, rid)
    # The fake planner ignores the edit, so the stricter grammar fails the plan: the edit is live.
    assert out["state"] == "failed"
    assert any("category 1 cannot use motif" in e for e in out["unmet_rules"])


def test_doctor_catches_a_motif_outside_the_vocabulary(root_copy):
    gpath = root_copy / "knowledge/set_grammar/v1.yaml"
    grammar = yaml.safe_load(gpath.read_text(encoding="utf-8"))
    grammar["category_motifs"]["4"] = ["landscape"]
    gpath.write_text(json.dumps(grammar), encoding="utf-8")
    report = collect(root_copy)
    assert report.has_errors
    assert any("landscape" in c.detail for c in report.checks)


def test_doctor_is_green_on_this_folder():
    report = collect(ROOT)
    assert not report.has_errors, [c for c in report.checks if c.status == "error"]
