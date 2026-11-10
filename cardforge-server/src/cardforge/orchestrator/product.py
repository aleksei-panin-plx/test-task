# ruff: noqa: E501  (tool descriptions and Russian messages)
"""The designer's flow over the core service (stage 7 plugin, user 2026-11-10).

One function per step of `docs/stages/07-plugin.md` §2; the MCP tools and the CLI only call
these. Decisions of the person are recorded verbatim (`reviewer`, `human_answer`). There is no
run budget in this flow: the run ceiling is the OpenRouter ledger remainder (the company's limit)
and the hidden safety is the attempt limit per card. There is no copyright checklist either:
policy QA doubts are shown as comments at G2/G3.

Rendering runs in a background worker (`cardforge work <run>`) so a tool call never blocks for
minutes; `CARDFORGE_WORKER=inline` runs it in the same process (tests, debugging).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path

import psutil
from dotenv import dotenv_values

from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.export import delivery
from cardforge.orchestrator import service, states
from cardforge.orchestrator.service import G1, G2, RUN_STOP, ServiceError
from cardforge.orchestrator.store import RunStore, now
from cardforge.orchestrator.workspace import Workspace, judge_profile, render_profile

WORKER = "worker.json"
PLANNING = {"created", "normalized", "planning_repair", "planned"}


def designer_config(root: Path) -> dict:
    cfg = tomllib.loads((root / "config/cardforge.toml").read_text(encoding="utf-8"))
    return {
        "variants": 3,
        "render": "primary",
        "shadow_judges": [],
        "max_attempts_per_card": 3,
        "render_key": "OPENROUTER_API_KEY",
        "chat_flag_limit": 12,
        **cfg.get("designer", {}),
    }


def render_key_present(root: Path) -> bool:
    key = designer_config(root)["render_key"]
    return bool(os.environ.get(key) or dotenv_values(root / ".env", interpolate=False).get(key))


def _store(ws: Workspace, run_id: str) -> RunStore:
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("manifest.json"):
        raise ServiceError(f"нет прогона {run_id} в {ws.runs_dir}")
    return store


def _paid(ws: Workspace, store: RunStore) -> bool:
    m = store.manifest()
    renders = [render_profile(ws, p) for p in (m.render_profiles or [m.render_profile])]
    renders += [render_profile(ws, p) for p in m.render_fallbacks.values()]
    return any(p.backend == "openrouter-image" for p in renders) or any(
        judge_profile(ws, j).provider == "openrouter" for j in [*m.deciders, *m.shadow_judges]
    )


def _ledger_room(root: Path) -> float:
    from cardforge.render.openrouter_image import openrouter_ledger

    try:
        remaining = float(openrouter_ledger(root)[1].summary()["remaining_usd"])
    except ValueError as exc:
        raise ServiceError(f"OpenRouter недоступен для рендера: {exc}") from None
    return math.floor(remaining * 100) / 100


# ---------------------------------------------------------------- 2.1 request → plan (G1)


def start(
    ws: Workspace,
    collection: str,
    *,
    sets: int | None = None,
    wishes: str = "",
    markets: list[str] | None = None,
    plan_mode: str = "collection",
    set_theme: str = "",
    variants: int | None = None,
    alternatives: int | None = None,
    run_id: str | None = None,
    client: str = "mcp",
    llm: str = "host-agent",
    render: str | None = None,
    g1: str | None = None,
    g2: str | None = None,
) -> dict:
    """Create a designer run and plan it up to G1 or the first host-agent task.

    `collection` is required and is never guessed (user 2026-11-10: the agent asks when the
    request is not unambiguous). Default plan: the whole collection — 16 set themes, sets 1–15
    with one card list each (object cards; gold slots marked), set 16 theme only; `sets` (1–16)
    named by the person plans only that many sets. `plan_mode="variants"` (3–5 variants of one
    `set_theme`) stays for development. Without the render key the run is plan-only. Gates:
    `config/autonomy.toml [gates]`, `g1`/`g2` override them. `llm`/`render`: offline tests only."""
    from cardforge.autonomy import gate_modes

    g1_mode, g2_mode = gate_modes(ws.root, g1, g2)
    if alternatives is not None:
        # User 2026-11-10: variants on request — 1 main (picked by the judges) + N alternatives.
        if not 1 <= alternatives <= 9:
            raise ServiceError("альтернатив — от 1 до 9 (по умолчанию 3)")
        plan_mode, variants = "variants", 1 + alternatives
    if not collection or not collection.strip():
        raise ServiceError("нужна тема коллекции: спроси человека («Лето», «Путешествие», «Кино»…)")
    if plan_mode == "variants" and not set_theme.strip():
        raise ServiceError("варианты набора: нужна тема набора (например «Пикник»); спроси человека")
    if sets is not None and not 1 <= sets <= 16:
        raise ServiceError("число сетов — от 1 до 16")
    cfg = designer_config(ws.root)
    full = render is not None or render_key_present(ws.root)
    options: dict = {"designer": True, **({"g3_unconfirmed": True} if g2_mode == "none" else {}),
                     **({"alternatives": alternatives} if alternatives is not None else {})}  # fmt: skip
    shadow: list[str] = []
    budget = 0.0
    if not full:
        render, options["plan_only"] = "fake", True
    else:
        render = render or cfg["render"]
        if render != "fake":
            shadow = list(cfg["shadow_judges"])
            budget = _ledger_room(ws.root)
    sets_total = sets or 16
    request = CollectionRequest(
        request_id=run_id or "new",
        collection_theme=collection.strip(),
        set_theme=set_theme.strip(),
        producer_wishes_raw=wishes,
        plan_mode=plan_mode,
        sets_total=sets_total,
        variants_count=(min(sets_total, 15) if plan_mode == "collection"
                        else variants or cfg["variants"]),
        markets=[m.upper() for m in markets or []],
        render_profile=render,
        hitl=Hitl(g1=g1_mode, g2=g2_mode),
        budget=Budget(max_cost_usd=budget, max_attempts_per_card=cfg["max_attempts_per_card"]),
    )  # fmt: skip
    rid = service.create_run(
        ws, request, llm=llm, render=render, judge="none", shadow_judges=shadow,
        run_id=run_id, client=client, runtime_options=options,
    )  # fmt: skip
    service.plan(ws, rid)
    return view(ws, rid)


def plan_view(ws: Workspace, run_id: str) -> dict:
    """G1 for the person: per variant title, idea, 10 rows, policy notes. No cost."""
    store = _store(ws, run_id)
    if not store.exists("plan/variants.json"):
        raise ServiceError("плана ещё нет: сначала ответь на задачи ядра (get_agent_tasks)")
    gate = store.read_json(G1) if store.exists(G1) else {"items": [], "decisions": {}}
    flagged = {i["variant_id"] for i in gate["items"] if i.get("flagged")}
    request = store.read_model("request.json", CollectionRequest)
    variants = []
    for p in store.read_json("plan/variants.json"):
        variants.append({
            "variant_id": p["variant_id"], "title": p["title"], "concept": p["concept"],
            "policy_flagged": p["variant_id"] in flagged,
            "policy_notes": p.get("policy_notes", []),
            "decision": (gate["decisions"].get(p["variant_id"]) or {}).get("decision"),
            "cards": [
                {"card_id": c["card_id"], "object": c["object_name"], "category": c["category"],
                 "background": (f"{c['background']['motif']}/{c['background']['color']}"
                                if c.get("background") else None),
                 "policy_flags": [f"{f['rule_id']}: {f['verdict']}" for f in c.get("policy_flags", [])]}
                for c in p["cards"]
            ],
        })  # fmt: skip
    coll = store.read_json("plan/collection.json") if store.exists("plan/collection.json") else None
    for v, p in zip(variants, store.read_json("plan/variants.json"), strict=True):
        v["set_index"], v["gold_slots"] = p.get("set_index"), p.get("gold_slots", 0)
    return {
        "run_id": run_id,
        "brief": {"collection": request.collection_theme, "set_theme": request.set_theme,
                  "wishes": request.producer_wishes_raw, "markets": request.markets,
                  "plan_mode": request.plan_mode, "sets": request.sets_total},
        "collection": None if coll is None else {
            "set_themes": coll.get("set_themes", []),
            "grand_set_theme": coll.get("grand_set_theme"),
            "object_cards": sum(len(v["cards"]) for v in variants),
            "gold_slots": sum(v["gold_slots"] or 0 for v in variants),
        },
        "conflicts": [c.model_dump() for c in request.conflicts],
        "edits_so_far": [e["text"] for e in store.manifest().counters.get("plan_edits", [])],
        "variants": variants,
        "plan_only": bool(store.manifest().runtime_options.get("plan_only")),
    }  # fmt: skip


def edit_plan(ws: Workspace, run_id: str, text: str, reviewer: str) -> dict:
    """G1 edit in the person's words: the plan goes back to repair with the edit, the host agent
    rewrites it, validators and policies run again, G1 opens again."""
    if not text.strip() or not reviewer.strip():
        raise ServiceError("правка плана: нужны слова человека и его имя")
    store = _store(ws, run_id)
    m = store.manifest()
    if m.state != "awaiting_g1":
        raise ServiceError(f"план правится только на G1, прогон сейчас {m.state}")
    event = store.emit("human_decision", {
        "gate": "G1", "subject": "plan", "decision": "edit", "human_answer": text,
        "decided_by": "human", "reviewer": reviewer,
    })  # fmt: skip
    n = len(list(store.path("plan/rounds").glob("round_*.json")))

    def change(mm) -> None:
        mm.counters.setdefault("plan_edits", []).append(
            {"text": text, "reviewer": reviewer, "event_id": event.event_id, "round": n}
        )
        mm.counters["plan_round"] = n
        mm.counters["plan_edit_base"] = n

    store.update_manifest(change)
    store.path(G1).unlink(missing_ok=True)
    states.set_run_state(store, "planning_repair", f"G1 edit: {text}")
    service.plan(ws, run_id)
    return view(ws, run_id)


def approve_plan(
    ws: Workspace,
    run_id: str,
    reviewer: str,
    human_answer: str,
    decisions: dict[str, str] | None = None,
) -> dict:
    """«Запускай»: approve (or reject) variants at G1, then rendering starts in the background."""
    store = _store(ws, run_id)
    if store.manifest().state != "awaiting_g1":
        raise ServiceError(f"G1 не открыт, прогон сейчас {store.manifest().state}")
    gate = store.read_json(G1)
    decisions = decisions or {i["variant_id"]: "approve" for i in gate["items"]}
    notes = {v: human_answer for v in decisions}
    service.decide_gate(ws, run_id, "g1", decisions, reviewer, notes)
    if store.manifest().runtime_options.get("plan_only"):
        out = view(ws, run_id)
        out["message"] = (
            "План сохранён. Картинок не будет: нет ключа рендера (OPENROUTER_API_KEY), "
            "см. `doctor`."
        )
        return out
    return resume(ws, run_id)


# ---------------------------------------------------------------- 2.2 generation


def _worker_alive(store: RunStore) -> bool:
    if not store.exists(WORKER):
        return False
    info = store.read_json(WORKER)
    if info.get("finished"):
        return False
    pid = info.get("pid")
    try:
        return bool(pid) and psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except psutil.Error:
        return False


def work(ws: Workspace, run_id: str) -> dict:
    """The worker body: advance the run to its next wait, under the shared paid lock."""
    from cardforge.render.edit_runtime import paid_lock

    store = _store(ws, run_id)
    lock = paid_lock(ws.root / "runs/_api/paid-run.lock") if _paid(ws, store) else nullcontext()
    store.emit("worker", {"kind": "started", "pid": os.getpid()})
    try:
        with lock:
            result = service.advance(ws, run_id)
    except Exception as exc:  # the run stays resumable; the reason is journaled
        store.emit("error", {"kind": "worker_failed", "detail": f"{type(exc).__name__}: {exc}"})
        info = store.read_json(WORKER) if store.exists(WORKER) else {}
        store.write(WORKER, {**info, "finished": now(), "error": str(exc)})
        raise
    info = store.read_json(WORKER) if store.exists(WORKER) else {}
    store.write(WORKER, {**info, "finished": now(), "state": result["state"]})
    store.emit("worker", {"kind": "finished", "state": result["state"]})
    return result


def _start_worker(ws: Workspace, run_id: str) -> None:
    store = _store(ws, run_id)
    if _worker_alive(store):
        return
    if os.environ.get("CARDFORGE_WORKER") == "inline":
        store.write(WORKER, {"pid": os.getpid(), "started": now(), "inline": True})
        work(ws, run_id)
        return
    cmd = [sys.executable, "-m", "cardforge", "work", run_id, "--root", str(ws.root),
           "--runs-dir", str(ws.runs_dir)]  # fmt: skip
    with open(store.path("worker.log"), "a", encoding="utf-8") as log:
        detach = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )  # the worker outlives the MCP server and the agent session
        proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=ws.root, **detach,
        )  # fmt: skip
    store.write(WORKER, {"pid": proc.pid, "started": now(), "cmd": cmd})


def resume(ws: Workspace, run_id: str) -> dict:
    """«Продолжи»: planning steps run here (host-agent tasks, no paid calls), rendering in the
    background worker. Clears this run's stop file."""
    store = _store(ws, run_id)
    store.path(RUN_STOP).unlink(missing_ok=True)
    m = store.manifest()
    step = m.counters.get("resume_state") if m.state == "awaiting_agent" else m.state
    if step in PLANNING:
        service.plan(ws, run_id)
    elif m.state in ("awaiting_g1", "awaiting_g2") and view(ws, run_id)["waiting"]:
        pass  # the person decides first
    elif m.state != "failed" and (m.state not in states.RUN_TERMINAL or _has_work(store)):
        if m.state in states.RUN_TERMINAL:
            states.set_run_state(store, "rendering", "resume: cards still to render or review")
        _start_worker(ws, run_id)
    return view(ws, run_id)


def _has_work(store: RunStore) -> bool:
    return any(c.state in ("needs_fix", "spec_ready", "flagged") for c in store.manifest().cards.values())


def stop(ws: Workspace, run_id: str) -> dict:
    """«Стоп»: the run stops after the current call; `resume` continues it."""
    store = _store(ws, run_id)
    store.write(RUN_STOP, f"stopped {now()}\n")
    store.emit("error", {"kind": "stop_requested", "detail": "stop_run"})
    out = view(ws, run_id)
    out["message"] = "Остановка после текущего вызова; продолжить — resume_run."
    return out


def _pending_tasks(store: RunStore) -> list[str]:
    m = store.manifest()
    if m.state != "awaiting_agent":
        return []
    return [
        t for t in m.counters.get("pending_tasks", [])
        if not store.exists(t.replace(".task.json", ".answer.json"))
    ]  # fmt: skip


def agent_tasks(ws: Workspace, run_id: str) -> dict:
    """Tasks the core waits for (plan, normalise, policy screen, image inspection, fix
    normalisation). The host agent answers each with `answer_task`; never a render prompt."""
    store = _store(ws, run_id)
    tasks = []
    for rel in _pending_tasks(store):
        task = store.read_json(rel)
        images = [task["image"]] if task.get("image") else list(task.get("images") or [])
        tasks.append({"task": rel, "step": task["step"], "body": task,
                      "images": [str(store.path(i)) for i in images]})  # fmt: skip
    return {"run_id": run_id, "tasks": tasks,
            "how": "answer each with submit_agent_answer (fields of answer_schema), then resume_run"}  # fmt: skip


def answer_task(
    ws: Workspace, run_id: str, task_rel: str, answer: dict, provider: str, model: str
) -> dict:
    store = _store(ws, run_id)
    if task_rel not in _pending_tasks(store):
        raise ServiceError(f"{task_rel} не ждёт ответа")
    task = store.read_json(task_rel)
    body = {k: v for k, v in answer.items() if k not in ("task_sha256", "provider", "model")}
    store.write(task_rel.replace(".task.json", ".answer.json"),
                {**body, "task_sha256": task["task_sha256"], "provider": provider, "model": model})  # fmt: skip
    left = _pending_tasks(store)
    return {"answered": task_rel, "tasks_left": len(left),
            "next": "resume_run" if not left else "submit_agent_answer"}  # fmt: skip


def view(ws: Workspace, run_id: str) -> dict:
    """Run status for the person and the agent: progress and the next step, never the cost."""
    store = _store(ws, run_id)
    m = store.manifest()
    st = service.status(ws, run_id)
    st.pop("cost_usd", None)
    st.pop("ip_check", None)
    approved = set()
    if store.exists(G1):
        approved = {v for v, d in store.read_json(G1)["decisions"].items() if d["decision"] == "approve"}
    cards = [c for c in m.cards.values() if c.variant_id in approved]
    st["progress"] = {"accepted": sum(c.state == "accepted" for c in cards), "total": len(cards)}
    st["worker_running"] = _worker_alive(store)
    st["stopped"] = store.exists(RUN_STOP)
    st.setdefault("waiting", [])
    tasks = _pending_tasks(store)
    if tasks:
        nxt = "answer_tasks"
    elif st["stopped"] and m.state not in states.RUN_TERMINAL:
        nxt = "stopped"
    elif st["worker_running"]:
        nxt = "wait_render"
    elif m.state == "awaiting_g1":
        nxt = "show_plan"
    elif m.state == "awaiting_g2":
        nxt = "show_flags"
    elif m.state in ("completed", "partial") and _alternatives(store):
        fresh = store.exists("delivery/current/index.html") and not _delivery_stale(
            store, store.read_json("delivery/current/delivery.json")["variants"]
        )
        nxt = "review_variants" if fresh else "export"
    elif m.state in ("completed", "partial") and _collection(store):
        fresh = store.exists("delivery/current/index.html") and not _delivery_stale(
            store, store.read_json("delivery/current/delivery.json")["variants"]
        )
        nxt = "review_collection" if fresh else "export"
    elif m.state in ("completed", "partial"):
        sel = m.counters.get("g3_selection")
        if not sel:
            nxt = "choose_variant"
        elif not store.exists("delivery/current/index.html") or _delivery_stale(store, sel["variants"]):
            nxt = "export"  # a card changed after the delivery (regenerate): rebuild it
        else:
            nxt = "done"
    elif m.state == "failed":
        nxt = "failed"
    else:
        nxt = "resume"
    st["next"] = nxt
    st["pending_tasks"] = tasks
    st["plan_only"] = bool(m.runtime_options.get("plan_only"))
    return st


def _alternatives(store: RunStore) -> bool:
    """Variants on a producer's request: 1 main variant picked by the judges + N alternatives."""
    return "alternatives" in store.manifest().runtime_options


def rank_variants(store: RunStore) -> list[dict]:
    """The judges' order of the complete variants, from signals the run already has (no new call):
    set check (grammar), Kimi sheet review, CSD style consistency, cards not confirmed by the
    judges, GPT auto-pass. Best first."""
    m = store.manifest()
    complete = set(delivery.complete_variants(store))
    unconf = service.unconfirmed_cards(m)
    auto = {c for c, r in m.counters.get("auto_pass", {}).items() if not r.get("returned")}
    rows = []
    for p in store.read_json("plan/variants.json"):
        vid = p["variant_id"]
        ids = [c["card_id"] for c in p["cards"]]
        sq = store.read_json(f"sets/{vid}.json") if store.exists(f"sets/{vid}.json") else {}
        sheet = sq.get("sheet_review") if isinstance(sq.get("sheet_review"), dict) else {}
        style = sq.get("style_consistency") if isinstance(sq.get("style_consistency"), dict) else {}
        row = {
            "variant_id": vid, "title": p["title"], "complete": vid in complete,
            "set_check": sq.get("decision"),
            "sheet_outliers": len(sheet.get("outlier_cards") or []) if sheet.get("status") == "ok" else None,
            "style_dispersion": style.get("dispersion") if style.get("status") == "ok" else None,
            "unconfirmed": sum(c in unconf for c in ids),
            "auto_passed": sum(c in auto for c in ids),
        }
        row["key"] = (not row["complete"], row["set_check"] != "pass", row["sheet_outliers"] or 0,
                      row["unconfirmed"], -row["auto_passed"], row["style_dispersion"] or 1.0, vid)  # fmt: skip
        rows.append(row)
    rows.sort(key=lambda r: r["key"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r.pop("key")
    return rows


def _main_variant(ws: Workspace, store: RunStore) -> str | None:
    """The main variant: the person's G3 choice, else the judges' first complete variant."""
    m = store.manifest()
    main = m.counters.get("main_variant")
    complete = set(delivery.complete_variants(store))
    if main and main["variant"] in complete:
        return main["variant"]
    ranking = rank_variants(store)
    best = next((r["variant_id"] for r in ranking if r["complete"]), None)
    if best is None:
        return None
    record = {"variant": best, "by": "judges", "ranking": ranking, "at": now()}
    store.update_manifest(lambda mm: mm.counters.update(main_variant=record))
    store.emit("check_result", {"check": "variant_rank", "passed": None, "main": best, "ranking": ranking})
    return best


def _collection(store: RunStore) -> bool:
    return store.read_json("request.json").get("plan_mode") == "collection"


def _delivery_stale(store: RunStore, variants: list[str]) -> bool:
    complete = set(delivery.complete_variants(store))
    if not all(v in complete for v in variants):
        return False
    meta = json.loads(store.path("delivery/current/delivery.json").read_text(encoding="utf-8"))
    return meta.get("sources") != delivery._sources(store, [v for v in meta["variants"]])


def list_runs(ws: Workspace) -> dict:
    out = []
    for path in sorted(ws.runs_dir.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            req = json.loads((path.parent / "request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"run_id": path.parent.name, "state": m.get("state"),
                    "theme": f"{req.get('collection_theme')}: {req.get('set_theme')}"})  # fmt: skip
    return {"runs": out[:50]}


# ---------------------------------------------------------------- 2.3 G2 (flagged only)


def flags(ws: Workspace, run_id: str) -> dict:
    """Cards waiting for the person: reason in words and the policy QA comment."""
    store = _store(ws, run_id)
    if store.manifest().state != "awaiting_g2" or not store.exists(G2):
        return {"run_id": run_id, "cards": [], "message": "на G2 сейчас ничего не ждёт"}
    gate = store.read_json(G2)
    plans = {c["card_id"]: (p["title"], c) for p in store.read_json("plan/variants.json") for c in p["cards"]}
    m = store.manifest()
    cards = []
    for item in gate["items"]:
        cid = item["card_id"]
        if cid in gate["decisions"]:
            continue
        title, spec = plans.get(cid, ("", {}))
        cards.append({
            "card_id": cid, "variant": f"{item['variant_id']} «{title}»", "object": item["object"],
            "category": spec.get("category"), "attempt": m.cards[cid].attempts,
            "why": [delivery.reason_ru(r) for r in item.get("reasons") or [] if r] or ["на проверку человеку"],
            "policy_notes": item.get("policy_notes") or delivery.policy_notes(store, cid),
            "regenerated_on_request": cid in m.counters.get("human_only", {}),
            "image": str(store.path(item["image"])) if item.get("image") else None,
        })  # fmt: skip
    limit = designer_config(ws.root)["chat_flag_limit"]
    return {"run_id": run_id, "cards": cards, "in_chat": len(cards) <= limit,
            "how": "accept | reject | fix: <слова человека> по каждой карте → submit_card_decisions"}  # fmt: skip


def decide_cards(
    ws: Workspace,
    run_id: str,
    reviewer: str,
    decisions: dict[str, str] | None = None,
    from_file: Path | None = None,
) -> dict:
    """G2 decisions in the person's words (or the review page's JSON), then the run goes on."""
    if from_file is not None:
        data = json.loads(Path(from_file).read_text(encoding="utf-8"))
        if data.get("page_id"):
            from cardforge.export.review_page import from_blind_export

            mapped = from_blind_export(_store(ws, run_id), data)
            service.decide_gate(ws, run_id, "g2", mapped["decisions"], reviewer or data.get("reviewer", ""),
                                mapped["notes"], mapped["reasons"], blind=True)  # fmt: skip
        else:
            service.decide_gate(ws, run_id, "g2", data["decisions"], reviewer or data.get("reviewer", ""),
                                data.get("notes"), data.get("reasons"))  # fmt: skip
    elif decisions:
        service.decide_gate(ws, run_id, "g2", decisions, reviewer)
    else:
        raise ServiceError("нужны решения по картам или файл страницы проверки")
    if view(ws, run_id)["waiting"]:
        return view(ws, run_id)
    return resume(ws, run_id)


def review_page(ws: Workspace, run_id: str) -> dict:
    from cardforge.export.review_page import write_review_page

    store = _store(ws, run_id)
    if not store.exists(G2):
        raise ServiceError("G2 не открыт для этого прогона")
    path = write_review_page(store, blind=True, root=ws.root)
    return {"page": str(path), "how": "человек открывает страницу, отмечает карты и сохраняет JSON; "
            "его путь — в submit_card_decisions(from_file=…)"}  # fmt: skip


# ---------------------------------------------------------------- 2.4–2.5 G3 and delivery


def sheets(ws: Workspace, run_id: str) -> dict:
    """Per rendered variant: its 5×2 sheet, completeness, auto-passed cards, policy comments."""
    store = _store(ws, run_id)
    m = store.manifest()
    if not store.exists(G1):
        raise ServiceError("варианты ещё не рисовались")
    approved = [v for v, d in store.read_json(G1)["decisions"].items() if d["decision"] == "approve"]
    if any(not store.exists(f"sheets/{v}.png") for v in approved):
        service.export(ws, run_id)
    complete = set(delivery.complete_variants(store))
    auto = {c for c, r in m.counters.get("auto_pass", {}).items() if not r.get("returned")}
    out = []
    for p in store.read_json("plan/variants.json"):
        vid = p["variant_id"]
        if vid not in approved:
            continue
        ids = [c["card_id"] for c in p["cards"]]
        notes = [f"{cid}: {n}" for cid in ids for n in delivery.policy_notes(store, cid, m.cards[cid].accepted_attempt)]
        out.append({
            "variant_id": vid, "set_index": p.get("set_index"), "gold_slots": p.get("gold_slots", 0),
            "title": p["title"], "concept": p["concept"],
            "complete": vid in complete,
            "accepted": sum(m.cards[c].state == "accepted" for c in ids),
            "not_ready": [c for c in ids if m.cards[c].state != "accepted"],
            "auto_passed": sorted(c for c in ids if c in auto),
            "unconfirmed": [
                {"card_id": c, "object": next(x["object_name"] for x in p["cards"] if x["card_id"] == c),
                 "why": [delivery.reason_ru(r) for r in rec["reasons"]] or [delivery.reason_ru(rec["why"])],
                 "policy_notes": rec.get("policy_notes", [])}
                for c, rec in sorted(service.unconfirmed_cards(m).items()) if c in ids
            ],
            "policy_notes": notes,
            "sheet": str(store.path(f"sheets/{vid}.png")),
        })  # fmt: skip
    sel = m.counters.get("g3_selection")
    if _alternatives(store):
        main = _main_variant(ws, store)
        ranks = {r["variant_id"]: r for r in rank_variants(store)}
        for row in out:
            row["main"] = row["variant_id"] == main
            row["judges"] = ranks.get(row["variant_id"])
        out.sort(key=lambda r: (not r["main"], ranks.get(r["variant_id"], {}).get("rank", 99)))
        rec = store.manifest().counters.get("main_variant", {})
        return {"run_id": run_id, "main_variant": main, "main_by": rec.get("by"), "variants": out,
                "how": "основной вариант выбрали судьи (main=true), остальные — альтернативы; человек может "
                       "сделать основным другой (select_variant с одним вариантом) или перерисовать карту "
                       "(regenerate_cards)"}  # fmt: skip
    if _collection(store):
        coll = store.read_json("plan/collection.json")
        return {"run_id": run_id, "collection": store.read_json("request.json")["collection_theme"],
                "grand_set_theme": coll.get("grand_set_theme"), "sets": out,
                "how": "G3 коллекции: по каждому сету — лист, «не подтверждена судьями» с причиной; "
                       "принять, перерисовать с правкой (regenerate_cards) или заменить альтернативой "
                       "вручную (alternatives.md); выбора варианта нет"}  # fmt: skip
    return {"run_id": run_id, "variants": out, "selected": sel["variants"] if sel else None,
            "how": "человек называет вариант(ы) → select_variant; карту «не подтверждена» — принять как есть, "
                   "перерисовать с правкой (regenerate_cards) или заменить альтернативой вручную (alternatives.md)"}  # fmt: skip


def select_variants(
    ws: Workspace, run_id: str, variants: list[str], reviewer: str, human_answer: str
) -> dict:
    """G3: the person's choice of the final variant(s), verbatim (core `select_final`); the
    other variants no longer block `completed`; then the delivery is built."""
    if not variants or not reviewer.strip() or not human_answer.strip():
        raise ServiceError("выбор G3: нужны варианты, имя человека и его слова")
    store = _store(ws, run_id)
    if _collection(store):
        raise ServiceError("в плане коллекции выбора варианта нет: все сеты идут в выдачу (export_run)")
    if _alternatives(store):
        # G3: the person makes another variant the main one; the others stay alternatives.
        if len(variants) != 1:
            raise ServiceError("основным можно сделать один вариант")
        if variants[0] not in set(delivery.complete_variants(store)):
            raise ServiceError(f"{variants[0]}: вариант неполный — основным может быть только полный")
        event = store.emit("human_decision", {
            "gate": "G3", "subject": "variant", "decision": "main", "variants": variants,
            "human_answer": human_answer, "decided_by": "human", "reviewer": reviewer,
        })  # fmt: skip
        prev = store.manifest().counters.get("main_variant", {})
        record = {"variant": variants[0], "by": "human", "reviewer": reviewer, "human_answer": human_answer,
                  "event_id": event.event_id, "judges_pick": prev.get("variant") if prev.get("by") == "judges"
                  else prev.get("judges_pick"), "at": now()}  # fmt: skip
        store.update_manifest(lambda m: m.counters.update(main_variant=record))
        return export_run(ws, run_id)
    complete = set(delivery.complete_variants(store))
    missing = [v for v in variants if v not in complete]
    if missing:
        raise ServiceError(f"неполные варианты: {', '.join(missing)} — сначала все 10 карт должны быть приняты")
    result = service.select_final(ws, run_id, variants, reviewer, human_answer)
    out = export_run(ws, run_id)
    out["state"] = store.manifest().state
    out["dropped"] = result["dropped"]
    return out


def export_run(ws: Workspace, run_id: str) -> dict:
    """Sheets, framed previews, report; screened-out cards and alternatives; after the G3 choice
    the delivery folder (a changed one is versioned)."""
    store = _store(ws, run_id)
    out = service.export(ws, run_id)
    sel = store.manifest().counters.get("g3_selection")
    complete = set(delivery.complete_variants(store))
    main = None
    if _collection(store):
        # Collection mode (user 2026-11-10): no variant choice — every drawn set is delivered.
        approved = [v for v, d in store.read_json(G1)["decisions"].items() if d["decision"] == "approve"]
        sel = {"variants": sorted(approved)}
    elif _alternatives(store):
        # Variants on request: the main variant (judges, or the person at G3) + the alternatives.
        main = _main_variant(ws, store)
        sel = {"variants": ([main] if main else []) + sorted(v for v in complete if v != main)}
    final = [v for v in sel["variants"] if v in complete] if sel else None
    out["screened"] = delivery.write_screened(store, final)
    out["screened_out"] = str(store.path("screened_out.html"))
    out["alternatives"] = str(store.path("alternatives.md"))
    if sel:
        waiting = [v for v in sel["variants"] if v not in complete]
        if final:
            out.update(delivery.write_delivery(store, final, main=main))
            if main:
                rec = store.manifest().counters["main_variant"]
                out["main_variant"] = {"variant": main, "by": rec["by"]}
        if waiting:
            out["waiting_variants"] = waiting
            out["message"] = (
                f"в {', '.join(waiting)} есть карты на перерисовке или проверке: выдача обновится, "
                "когда они будут приняты"
            )
    return out


# ---------------------------------------------------------------- 2.6 regenerate with edits


def regenerate(ws: Workspace, run_id: str, cards: list[str], note: str, reviewer: str) -> dict:
    """Redraw cards with the person's edit, also after export. The previous image stays an
    alternative; the card gets one attempt above the limit; the redraw always goes to the
    person (no auto-pass). An auto-passed card counts as a G3 return for the breaker."""
    if not cards or not note.strip() or not reviewer.strip():
        raise ServiceError("перегенерация: нужны карты, правка словами человека и его имя")
    store = _store(ws, run_id)
    m = store.manifest()
    if _worker_alive(store) or m.state in ("rendering", "created", "normalized", "planned",
                                           "planning_repair", "awaiting_g1", "awaiting_agent"):
        raise ServiceError(f"прогон сейчас {m.state}: дождись конца генерации или G2")  # fmt: skip
    for cid in cards:
        card = m.cards.get(cid)
        if card is None:
            raise ServiceError(f"нет карты {cid}")
        if card.state not in ("accepted", "rejected", "flagged") or not card.attempts:
            raise ServiceError(f"{cid}: {card.state}, перегенерировать можно нарисованную карту")
    request = store.read_model("request.json", CollectionRequest)
    event = store.emit("human_decision", {
        "gate": "regenerate", "subject": "card", "decision": "fix", "cards": cards,
        "human_answer": note, "decided_by": "human", "reviewer": reviewer,
    })  # fmt: skip
    for cid in cards:
        m = store.manifest()
        card = m.cards[cid]
        auto = m.counters.get("auto_pass", {}).get(cid)
        if card.state == "accepted" and auto and not auto.get("returned"):
            service.g3_return(ws, run_id, cid, f"regenerate: {note}", reviewer)
        elif card.state in ("accepted", "rejected"):
            states.set_card_state(store, cid, "flagged", f"regenerate: {note}")
            store.update_manifest(lambda mm, c=cid: setattr(mm.cards[c], "accepted_attempt", None))
        if store.exists(G2):
            gate = store.read_json(G2)
            old = gate["decisions"].pop(cid, None)
            if old is not None:
                gate.setdefault("history", []).append({"card_id": cid, **old, "superseded": f"regenerate: {note}"})
            gate["items"] = [i for i in gate["items"] if i["card_id"] != cid]
            store.write(G2, gate)
        attempts = store.manifest().cards[cid].attempts

        def grant(mm, c=cid, n=attempts) -> None:
            extra = mm.counters.setdefault("extra_attempts", {})
            extra[c] = max(extra.get(c, 0), n + 1 - request.budget.max_attempts_per_card)
            mm.counters.setdefault("human_only", {})[c] = {"event_id": event.event_id, "note": note}

        store.update_manifest(grant)
    ctx = service._context(ws, run_id, None)
    for cid in cards:
        entry = {"source": "human", "codes": [], "text": note, "reviewer": reviewer, "event_id": event.event_id}
        service._needs_fix(ctx, cid, [entry], same_seed=True)
    store.emit("cards_regenerated", {"cards": cards, "note": note, "reviewer": reviewer})
    m = store.manifest()
    if m.state in states.RUN_TERMINAL:
        states.set_run_state(store, "rendering", f"regenerate {', '.join(cards)}")
    elif m.state == "awaiting_g2":
        states.set_run_state(store, "rendering", f"regenerate {', '.join(cards)}")
    return resume(ws, run_id)
