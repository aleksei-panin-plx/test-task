# ruff: noqa: E501  (Russian messages)
"""CardForge Light run: request → normalised brief → collection plan → checks → G1 tables.

The whole flow of the Light plugin. It stops at G1: no render, judges, edits of images or
export. Every LLM step goes through the host agent (Claude Code / Codex answers task files,
`awaiting_agent`) or, offline, through `FakeLLM`. States:

    created → normalized → planning_repair* → awaiting_g1 → approved
    (any planning step may wait in `awaiting_agent`; `failed` = planning_failed)

The person's G1 edits are kept verbatim and go to the planner as `human_edits`; approval only
marks the tables as approved — nothing is drawn.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from cardforge.contracts.request import CollectionRequest, Hitl
from cardforge.llm.fake import FakeLLM
from cardforge.llm.provider import HostAgentProvider, LLMProvider, Pending
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.calls import BudgetExhausted
from cardforge.orchestrator.store import RunStore, now
from cardforge.orchestrator.tables import write_tables
from cardforge.orchestrator.workspace import Workspace
from cardforge.planner import planner
from cardforge.planner.collection import layout as collection_layout
from cardforge.policy.loader import resolve_active

STATE = "state.json"
MAX_STEPS = 12
LLMS = ("host-agent", "fake")


class ServiceError(ValueError):
    """A refusal the agent retells to the person."""


def _llm(name: str, options: dict | None = None) -> LLMProvider:
    if name == "host-agent":
        return HostAgentProvider()
    if name == "fake":
        return FakeLLM(**(options or {}))
    raise ServiceError(f"неизвестный планировщик {name!r}: {', '.join(LLMS)}")


def store_of(ws: Workspace, run_id: str) -> RunStore:
    store = RunStore(ws.run_dir(run_id))
    if not store.exists(STATE):
        raise ServiceError(f"нет прогона {run_id} в {ws.runs_dir}")
    return store


def state(store: RunStore) -> dict:
    return store.read_json(STATE)


def _set(store: RunStore, new_state: str, reason: str = "", **fields) -> dict:
    with store.lock:
        st = state(store) if store.exists(STATE) else {}
        old = st.get("state")
        st.update(fields, state=new_state, updated_at=now())
        if reason:
            st["reason"] = reason
        store.write(STATE, st)
    store.emit("state_changed", {"from": old, "to": new_state, "reason": reason})
    return st


def create(
    ws: Workspace,
    collection: str,
    *,
    sets: int | None = None,
    wishes: str = "",
    markets: list[str] | None = None,
    llm: str = "host-agent",
    run_id: str | None = None,
) -> str:
    """A new G1 run. `collection` is required and never guessed (user 2026-11-10)."""
    if not collection or not collection.strip():
        raise ServiceError("нужна тема коллекции: спроси человека («Лето», «Путешествие», «Кино»…)")
    if sets is not None and not 1 <= sets <= 16:
        raise ServiceError("число сетов — от 1 до 16")
    if llm not in LLMS:
        raise ServiceError(f"неизвестный планировщик {llm!r}: {', '.join(LLMS)}")
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    request = CollectionRequest(
        request_id=run_id,
        collection_theme=collection.strip(),
        set_theme="",
        producer_wishes_raw=wishes,
        sets_total=sets or 16,
        markets=[m.upper() for m in markets or []],
        hitl=Hitl(g1="all", g2="none"),
    )
    packs = resolve_active(ws.root, request.markets, request.policy_packs)
    store = RunStore(ws.run_dir(run_id))
    if store.exists(STATE):
        raise ServiceError(f"прогон {run_id} уже есть")
    store.write("request.json", request)
    _set(store, "created", created_at=now(), llm=llm, plan_round=0, plan_edit_base=0,
         plan_edits=[], approved=None,
         versions={"policies": [{"id": p.policy_id, "version": p.policy_version} for p in packs],
                   "plan_schema": planner.PLAN_SCHEMA})  # fmt: skip
    return run_id


def advance(ws: Workspace, run_id: str, llm: LLMProvider | None = None) -> dict:
    """Plan until G1, a host-agent wait or a failure; returns `view`."""
    store = store_of(ws, run_id)
    st = state(store)
    llm = llm or _llm(st["llm"])
    request = CollectionRequest.model_validate(store.read_json("request.json"))
    packs = resolve_active(ws.root, request.markets, request.policy_packs)
    ctx = planner.PlanContext(ws.root, store, BudgetLedger(store, request.budget.max_cost_usd), llm, packs)
    for _ in range(MAX_STEPS):
        st = state(store)
        current = st["state"]
        if current == "awaiting_agent":
            current = st.get("resume_state", "created")
        if current not in ("created", "normalized", "planning_repair"):
            break
        try:
            step = _normalize if current == "created" else _plan
            if step(ctx, store, request, current) == "wait":
                break
        except BudgetExhausted:
            _set(store, "failed", "budget_exhausted")
            break
        request = CollectionRequest.model_validate(store.read_json("request.json"))
    return view(ws, run_id)


def _wait(store: RunStore, pending: Pending, resume_state: str) -> str:
    _set(store, "awaiting_agent", pending.reason, resume_state=resume_state,
         pending_tasks=[pending.task_path])  # fmt: skip
    return "wait"


def _normalize(ctx: planner.PlanContext, store: RunStore, request: CollectionRequest, current: str) -> str:
    result = planner.normalize(ctx, request)
    if isinstance(result, Pending):
        return _wait(store, result, "created")
    store.write("request.json", result)
    _set(store, "normalized", pending_tasks=[])
    return "next"


def _plan(ctx: planner.PlanContext, store: RunStore, request: CollectionRequest, current: str) -> str:
    st = state(store)
    round_no = st.get("plan_round", 0)
    repair = None
    if round_no:
        previous = store.read_json(f"plan/rounds/round_{round_no - 1}.json")
        repair = {"plan": previous["raw"], "errors": previous["errors"],
                  "collection": previous.get("collection")}  # fmt: skip
        if st.get("plan_edits"):
            repair["human_edits"] = [e["text"] for e in st["plan_edits"]]
    result = planner.compose(ctx, request, round_no, repair)
    if isinstance(result, Pending):
        return _wait(store, result, current)
    verdicts = result["verdicts"]
    store.write(f"plan/rounds/round_{round_no}.json", {
        "raw": result["raw"], "collection": result.get("collection"), "errors": result["errors"],
        "warnings": result["warnings"], "verdicts": [v.model_dump(mode="json") for v in verdicts],
    })  # fmt: skip
    if result["errors"]:
        # A G1 edit starts a new repair series: the limit counts rounds since the last edit.
        if round_no - st.get("plan_edit_base", 0) >= planner.MAX_REPAIR_ROUNDS:
            store.write("plan/planning_failed.json", {"rounds": round_no + 1, "unmet_rules": result["errors"]})
            _set(store, "failed", "planning_failed", pending_tasks=[])
            return "wait"
        _set(store, "planning_repair", f"{len(result['errors'])} errors", plan_round=round_no + 1,
             pending_tasks=[])  # fmt: skip
        return "next"
    sets = planner.finalize(result["drafts"], verdicts)
    store.write("plan/sets.json", sets)
    store.write("plan/collection.json", {
        "collection_theme": request.collection_theme, **(result.get("collection") or {}),
        "layout": collection_layout(ctx.root, request.sets_total),
    })  # fmt: skip
    store.write("plan/validation.json", {"valid": True, "rounds": round_no + 1, "warnings": result["warnings"]})
    for v in verdicts:
        if v.verdict != "pass" and v.target != "request":
            store.emit("policy_verdict", v.model_dump(mode="json"), card_id=v.target)
    _set(store, "awaiting_g1", "plan ready", pending_tasks=[], approved=None)
    write_tables(store)
    return "wait"


# ---------------------------------------------------------------- G1 for the person


def edit_plan(ws: Workspace, run_id: str, text: str, reviewer: str, llm: LLMProvider | None = None) -> dict:
    """G1 edit in the person's words: the plan goes back to repair with the edit."""
    if not text.strip() or not reviewer.strip():
        raise ServiceError("правка плана: нужны слова человека и его имя")
    store = store_of(ws, run_id)
    st = state(store)
    if st["state"] not in ("awaiting_g1", "approved"):
        raise ServiceError(f"план правится только на G1, прогон сейчас {st['state']}")
    event = store.emit("human_decision", {"gate": "G1", "subject": "plan", "decision": "edit",
                                          "human_answer": text, "decided_by": "human", "reviewer": reviewer})  # fmt: skip
    n = len(list(store.path("plan/rounds").glob("round_*.json")))
    edits = [*st.get("plan_edits", []), {"text": text, "reviewer": reviewer, "event_id": event["event_id"], "round": n}]
    _set(store, "planning_repair", f"G1 edit: {text}", plan_edits=edits, plan_round=n, plan_edit_base=n, approved=None)
    return advance(ws, run_id, llm)


def approve_plan(ws: Workspace, run_id: str, reviewer: str, human_answer: str) -> dict:
    """«Ок» на G1: the tables are marked approved. Light draws nothing."""
    if not reviewer.strip() or not human_answer.strip():
        raise ServiceError("утверждение плана: нужны слова человека и его имя")
    store = store_of(ws, run_id)
    st = state(store)
    if st["state"] != "awaiting_g1":
        raise ServiceError(f"утвердить можно только план на G1, прогон сейчас {st['state']}")
    store.emit("human_decision", {"gate": "G1", "subject": "plan", "decision": "approve",
                                  "human_answer": human_answer, "decided_by": "human", "reviewer": reviewer})  # fmt: skip
    _set(store, "approved", "G1 approved", approved={"reviewer": reviewer, "human_answer": human_answer, "at": now()})
    write_tables(store)
    return view(ws, run_id)


# ---------------------------------------------------------------- host-agent tasks


def agent_tasks(ws: Workspace, run_id: str) -> dict:
    store = store_of(ws, run_id)
    st = state(store)
    tasks = []
    if st["state"] == "awaiting_agent":
        for rel in st.get("pending_tasks", []):
            tasks.append({"task": rel, **store.read_json(rel)})
    return {"run_id": run_id, "state": st["state"], "tasks": tasks,
            "how": "ответь на каждую задачу строго по answer_schema → submit_agent_answer → resume_run"}  # fmt: skip


def answer_task(ws: Workspace, run_id: str, task: str, answer: dict, provider: str, model: str) -> dict:
    store = store_of(ws, run_id)
    st = state(store)
    if task not in st.get("pending_tasks", []):
        raise ServiceError(f"задача {task} не ждёт ответа (ждут: {st.get('pending_tasks', [])})")
    body = store.read_json(task)
    if not str(provider).strip() or not str(model).strip():
        raise ServiceError("ответ должен назвать provider и model")
    envelope = {**answer, "task_sha256": body["task_sha256"], "provider": provider, "model": model,
                "schema_version": body.get("schema_version")}  # fmt: skip
    store.write(task.replace(".task.json", ".answer.json"), envelope)
    return {"run_id": run_id, "saved": task, "next": "resume_run"}


# ---------------------------------------------------------------- views


def _next(st: dict) -> str:
    return {
        "awaiting_agent": "answer_tasks",
        "awaiting_g1": "show_plan",
        "approved": "done",
        "failed": "failed",
    }.get(st["state"], "resume_run")


def view(ws: Workspace, run_id: str) -> dict:
    store = store_of(ws, run_id)
    st = state(store)
    request = CollectionRequest.model_validate(store.read_json("request.json"))
    out = {
        "run_id": run_id,
        "state": st["state"],
        "next": _next(st),
        "collection": request.collection_theme,
        "sets": request.sets_total,
        "pending_tasks": st.get("pending_tasks", []) if st["state"] == "awaiting_agent" else [],
    }
    if st["state"] == "failed":
        out["reason"] = st.get("reason")
        if store.exists("plan/planning_failed.json"):
            out["unmet_rules"] = store.read_json("plan/planning_failed.json")["unmet_rules"]
    if st["state"] in ("awaiting_g1", "approved"):
        out["tables"] = {k: str(store.path(v)) for k, v in TABLE_FILES.items()}
    return out


TABLE_FILES = {"markdown": "tables/plan.md", "csv": "tables/cards.csv", "json": "tables/plan.json"}


def plan_view(ws: Workspace, run_id: str) -> dict:
    """G1 for the person: set themes, per set the content table (Markdown), files."""
    store = store_of(ws, run_id)
    if not store.exists("tables/plan.json"):
        raise ServiceError("плана ещё нет: сначала ответь на задачи ядра (get_agent_tasks → resume_run)")
    data = store.read_json("tables/plan.json")
    return {
        "run_id": run_id,
        "state": state(store)["state"],
        "markdown": store.path("tables/plan.md").read_text(encoding="utf-8"),
        "files": {k: str(store.path(v)) for k, v in TABLE_FILES.items()},
        "collection": data["collection"],
        "conflicts": data["conflicts"],
        "warnings": data["warnings"],
        "edits_so_far": data["edits_so_far"],
    }


def list_runs(ws: Workspace) -> dict:
    rows = []
    if ws.runs_dir.exists():
        for d in sorted(ws.runs_dir.iterdir(), reverse=True):
            if (d / STATE).exists() and (d / "request.json").exists():
                st = RunStore(d).read_json(STATE)
                req = RunStore(d).read_json("request.json")
                rows.append({"run_id": d.name, "collection": req["collection_theme"],
                             "sets": req["sets_total"], "state": st["state"]})  # fmt: skip
    return {"runs": rows[:20]}


def resume(ws: Workspace, run_id: str) -> dict:
    return advance(ws, run_id)


def run_root(ws: Workspace, run_id: str) -> Path:
    return store_of(ws, run_id).root
