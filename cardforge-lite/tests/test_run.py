# ruff: noqa: E501
"""The G1 run end to end: fake planner, host-agent tasks, repair, edits, approval, tables."""

from __future__ import annotations

import csv
import io
import json

import pytest

from cardforge.llm.fake import FakeLLM
from cardforge.orchestrator import run
from cardforge.orchestrator.store import RunStore


def _fake_run(ws, sets, **kw):
    rid = run.create(ws, "Лето", sets=sets, llm="fake", **kw)
    return rid, run.advance(ws, rid)


def test_two_sets_reach_g1_with_tables(ws):
    rid, out = _fake_run(ws, 2)
    assert out["state"] == "awaiting_g1" and out["next"] == "show_plan"
    view = run.plan_view(ws, rid)
    md = view["markdown"]
    assert "## Сет 1." in md and "## Сет 2." in md and "## Сет 3." not in md
    assert "| № | Объект | Основной объект | Категория | Окружение | Фон | Политики |" in md
    assert "Золотых позиций: 0." in md
    doc = json.loads(open(view["files"]["json"], encoding="utf-8").read())
    assert doc["status"] == "draft"
    assert doc["collection"]["object_cards"] == 20 and doc["collection"]["gold_slots"] == 0
    cards = [c for s in doc["sets"] for c in s["cards"]]
    assert all(c["main_object"] for c in cards)
    assert all((c["environment"] is None) == (c["category"] != 4) for c in cards)
    assert all(c["background"]["motif"] == "scene" for c in cards if c["category"] == 4)


def test_full_collection_marks_gold_slots_and_grand_set(ws):
    rid, out = _fake_run(ws, None)
    assert out["state"] == "awaiting_g1"
    doc = json.loads(open(run.plan_view(ws, rid)["files"]["json"], encoding="utf-8").read())
    gold = [sum(c["slot"] == "gold" for c in s["cards"]) for s in doc["sets"][:15]]
    assert gold == [0] * 6 + [1, 1, 2, 2, 2, 2, 3, 3, 4]
    assert all(len(s["cards"]) == 10 for s in doc["sets"][:15])
    grand = doc["sets"][15]
    assert grand["grand_set"] and grand["cards"] == [] and grand["theme"] == doc["collection"]["grand_set_theme"]
    md = run.plan_view(ws, rid)["markdown"]
    assert "★ золотая позиция" in md and "Гранд-сет: только тема" in md
    rows = list(csv.DictReader(io.StringIO(open(run.plan_view(ws, rid)["files"]["csv"], encoding="utf-8-sig").read())))
    assert {r["slot"] for r in rows} == {"object", "gold", "grand_set"}
    assert sum(r["slot"] == "gold" for r in rows) == 20
    obj = [r for r in rows if r["slot"] == "object"]
    assert all(r["environment"] == "—" for r in obj if r["category"] != "4")
    assert all(r["environment"] != "—" for r in obj if r["category"] == "4")
    assert set(rows[0]) >= {"main_object", "environment", "background_motif", "background_color", "policy_flags"}


def test_repair_round_fixes_a_bad_plan(ws):
    rid = run.create(ws, "Лето", sets=1, llm="fake")
    out = run.advance(ws, rid, FakeLLM(bad_plans=1))
    assert out["state"] == "awaiting_g1"
    store = RunStore(ws.run_dir(rid))
    assert store.read_json("plan/rounds/round_0.json")["errors"]
    assert store.read_json("plan/validation.json")["rounds"] == 2


def test_planning_fails_after_repair_limit(ws):
    rid = run.create(ws, "Лето", sets=1, llm="fake")
    out = run.advance(ws, rid, FakeLLM(bad_plans=9))
    assert out["state"] == "failed" and out["next"] == "failed"
    assert out["reason"] == "planning_failed" and out["unmet_rules"]


def test_host_agent_tasks_edit_and_approve(ws):
    """The MCP path: the agent answers the core's task files, the person edits and approves."""
    rid = run.create(ws, "Кино", sets=1)
    fake = FakeLLM()
    out = run.advance(ws, rid)
    steps = []
    for _ in range(10):
        if out["next"] != "answer_tasks":
            break
        for t in run.agent_tasks(ws, rid)["tasks"]:
            steps.append(t["step"])
            answer = getattr(fake, "_" + t["step"])(t)
            run.answer_task(ws, rid, t["task"], answer, "claude-code", "test-model")
        out = run.resume(ws, rid)
    assert steps == ["normalize", "plan", "policy_screen"]
    assert out["next"] == "show_plan"
    task = RunStore(ws.run_dir(rid)).read_json("tasks/plan.task.json")
    assert "main_object" in json.dumps(task["answer_schema"]) and "category_motifs" in task["set_grammar"]
    assert set(task["vocabulary"]["category"]) == {"1", "2", "3", "4"}

    out = run.edit_plan(ws, rid, "в сете 1 больше пляжа", "Аня")
    assert out["next"] == "answer_tasks"
    tasks = run.agent_tasks(ws, rid)["tasks"]
    assert tasks[0]["step"] == "repair" and tasks[0]["human_edits"] == ["в сете 1 больше пляжа"]
    for _ in range(5):
        for t in run.agent_tasks(ws, rid)["tasks"]:
            run.answer_task(ws, rid, t["task"], getattr(fake, "_" + t["step"])(t), "claude-code", "m")
        out = run.resume(ws, rid)
        if out["next"] != "answer_tasks":
            break
    view = run.plan_view(ws, rid)
    assert view["edits_so_far"] == ["в сете 1 больше пляжа"]
    assert "Учтены правки: в сете 1 больше пляжа" in view["markdown"]

    out = run.approve_plan(ws, rid, "Аня", "ок, утверждаю")
    assert out["state"] == "approved" and out["next"] == "done"
    assert "Статус: утверждён" in run.plan_view(ws, rid)["markdown"]
    assert json.loads(open(out["tables"]["json"], encoding="utf-8").read())["approved"]["human_answer"] == "ок, утверждаю"


def test_stale_task_answer_is_refused(ws):
    rid = run.create(ws, "Кино", sets=1)
    run.advance(ws, rid)
    with pytest.raises(run.ServiceError, match="не ждёт ответа"):
        run.answer_task(ws, rid, "tasks/plan.task.json", {}, "claude-code", "m")


@pytest.mark.parametrize("kw, msg", [({"collection": " "}, "тема коллекции"), ({"collection": "Лето", "sets": 17}, "от 1 до 16")])
def test_bad_requests(ws, kw, msg):
    with pytest.raises(run.ServiceError, match=msg):
        run.create(ws, **kw)


def test_china_market_pack_is_active(ws):
    rid, out = _fake_run(ws, 1, markets=["cn"])
    assert out["state"] == "awaiting_g1"
    state = RunStore(ws.run_dir(rid)).read_json("state.json")
    assert {p["id"] for p in state["versions"]["policies"]} == {"ip.baseline", "culture.cn"}


def test_list_runs(ws):
    rid, _ = _fake_run(ws, 1)
    assert run.list_runs(ws)["runs"][0]["run_id"] == rid
