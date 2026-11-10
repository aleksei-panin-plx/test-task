# ruff: noqa: E501  (Russian messages)
"""The core MCP tool set (`cardforge.mcp.tools`, the same 18 tools as the local plugin) behind
the server's identity, scopes and run ownership.

What the server adds and changes, nothing else:
- scope per tool (`read` / `run` / `decide` / `export`); a run of another owner does not exist;
- `reviewer` of every decision is the key's owner (decisions are bound to the user, plan §3.3);
  decisions are mirrored into the index (`decisions`) with channel and tool;
- `start_run` records the owner, `deployment = server`; `list_runs` lists only own runs;
- `submit_card_decisions(from_file=…)` is refused (no server paths from a client): the G2 page
  posts its decisions itself; `open_review_page` returns the page URL instead of a local path;
- G3 actions (`select_variant`, `regenerate_cards`, `export_run`) are allowed only from the
  channels in `CARDFORGE_G3_CHANNELS` (default `mcp,web`, like the local plugin; `web` = only the
  browser, portal plan §4);
- `get_run` adds the queue position and the gate page links. Cost is never shown (user 2026-11-10).
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict

from cardforge.mcp import tools as core_tools
from cardforge.orchestrator import product
from cardforge.orchestrator.workspace import Workspace
from server.auth import Principal
from server.core_hooks import acting_as
from server.settings import Settings
from server.store_index import Index

SCOPE = {
    "doctor": "read", "get_plan": "read", "get_run": "read", "list_runs": "read",
    "get_agent_tasks": "read", "get_flags": "read", "get_sheets": "read", "open_review_page": "read",
    "start_run": "run", "submit_agent_answer": "run", "resume_run": "run", "stop_run": "run",
    "edit_plan": "decide", "approve_plan": "decide", "submit_card_decisions": "decide",
    "select_variant": "decide", "regenerate_cards": "decide",
    "export_run": "export",
}  # fmt: skip
G3_TOOLS = {"select_variant", "regenerate_cards", "export_run"}
GATE = {"edit_plan": "G1", "approve_plan": "G1", "submit_card_decisions": "G2",
        "select_variant": "G3", "regenerate_cards": "G3", "export_run": "G3"}  # fmt: skip
MUTATING = set(SCOPE) - {k for k, v in SCOPE.items() if v == "read"}
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def describe() -> list[dict]:
    return core_tools.describe()


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def pages(settings: Settings, run_id: str) -> dict:
    base = f"{settings.public_url}/web/runs/{run_id}"
    return {"g1": f"{base}/g1", "g2": f"{base}/g2", "g3": f"{base}/g3", "login": f"{settings.public_url}/web/login"}


class ServerTools:
    def __init__(self, settings: Settings, index: Index, ws: Workspace):
        self.s = settings
        self.index = index
        self.ws = ws

    def call(self, p: Principal, name: str, arguments: dict | None) -> dict:
        """Every tool call of a verified owner is one usage event (channel mcp or web)."""
        from server import usage

        out = self._call(p, name, arguments)
        rid = (arguments or {}).get("run_id") or (out.get("structuredContent") or {}).get("run_id")
        prefix = "tool" if p.channel == "mcp" else "web"
        usage.record(self.index, p.owner, p.channel, f"{prefix}:{name}", run_id=rid, ok=not out.get("isError"))
        return out

    def _call(self, p: Principal, name: str, arguments: dict | None) -> dict:
        args = dict(arguments or {})
        scope = SCOPE.get(name)
        if scope is None:
            return _error(f"нет инструмента {name}")
        if not p.can(scope):
            return _error(f"у ключа нет права «{scope}» для {name}")
        rid = args.get("run_id")
        if rid is not None and self.index.owner(rid) != p.owner:
            return _error(f"нет прогона {rid}")
        if rid is not None and self.index.state(rid) == "archived":
            return _error(f"прогон {rid} в архиве (хранение — {self.s.retention_days:g} дней после выдачи); "
                          "вернуть может администратор: python -m server.retention restore")
        if name in G3_TOOLS and p.channel not in self.s.g3_channels:
            return _error(f"{name}: решения G3 — только на странице {pages(self.s, rid)['g3']}")
        if "reviewer" in core_tools.BY_NAME[name].schema["properties"]:
            args["reviewer"] = p.owner
        if name == "submit_card_decisions" and args.get("from_file"):
            return _error("from_file на сервере не принимается: решения со страницы G2 отправляются самой страницей")
        if name == "list_runs":
            return self._list_runs(p)
        if name == "open_review_page":
            return self._review_page(rid)
        if name == "start_run" and self.s.demo:
            return self._demo_start(p, args)
        lock = _locks[rid] if (rid and name in MUTATING) else None
        with acting_as(p.owner, p.channel), (lock or _Null()):
            out = core_tools.call(self.ws, name, args)
        if out.get("isError"):
            return out
        result = out.get("structuredContent") or {}
        if name == "start_run":
            self.index.add_run(result["run_id"], p.owner, args.get("collection", ""), p.channel, result.get("state", ""))
        if name in GATE:
            self.index.record_decision(rid, GATE[name], p.owner, p.channel, name,
                                       {k: v for k, v in args.items() if k != "run_id"})  # fmt: skip
        target = rid or result.get("run_id")
        if target:
            self._refresh(target)
            if name in ("get_run", "start_run", "resume_run", "approve_plan", "submit_card_decisions", "regenerate_cards", "stop_run"):
                self._decorate(out, target)
        return out

    # ---------------------------------------------------------------- helpers

    def _refresh(self, run_id: str) -> None:
        from server.worker import refresh

        try:
            refresh(self.index, self.ws, run_id)
        except (OSError, ValueError):
            pass

    def _decorate(self, out: dict, run_id: str) -> None:
        from cardforge.orchestrator.store import RunStore
        from server.budget_users import LIMIT_MESSAGE, limit_reached

        job = self.index.job(run_id)
        extra = {"pages": pages(self.s, run_id)}
        if limit_reached(RunStore(self.ws.run_dir(run_id))):
            extra["message"] = LIMIT_MESSAGE
        if job and job["status"] in ("queued", "running"):
            extra["queue"] = {"status": job["status"], "position": self.index.queue_position(run_id)}
            sc = out.setdefault("structuredContent", {})
            # The core view cannot see the server queue: a queued run looks idle at its last gate.
            if sc.get("next") not in ("answer_tasks", "stopped"):
                sc["next"] = "wait_render"
        out.setdefault("structuredContent", {}).update(extra)
        out["content"][0]["text"] = json.dumps(out["structuredContent"], ensure_ascii=False, indent=1)

    def _list_runs(self, p: Principal) -> dict:
        rows = []
        for r in self.index.runs(p.owner)[:50]:
            self._refresh(r["run_id"])
        for r in self.index.runs(p.owner)[:50]:
            rows.append({"run_id": r["run_id"], "state": r["state"], "theme": r["theme"],
                         "awaiting": r["awaiting"], "created_at": r["created_at"]})  # fmt: skip
        result = {"runs": rows}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                "structuredContent": result}  # fmt: skip

    def _review_page(self, run_id: str) -> dict:
        from cardforge.orchestrator.store import RunStore

        if not RunStore(self.ws.run_dir(run_id)).exists("review/g2.json"):
            return _error("G2 не открыт для этого прогона")
        result = {"page": pages(self.s, run_id)["g2"],
                  "how": "человек открывает страницу (вход по своему ключу), отмечает карты и нажимает «Отправить решения»; прогон продолжится сам"}  # fmt: skip
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                "structuredContent": result}  # fmt: skip

    def _demo_start(self, p: Principal, args: dict) -> dict:
        """CARDFORGE_SERVER_DEMO=1: the same start_run on the fake planner and FakeBackend
        (offline e2e, `cardforge demo` analogue through MCP); never in production."""
        from cardforge.orchestrator.service import ServiceError

        try:
            with acting_as(p.owner, p.channel):
                result = product.start(self.ws, args["collection"], sets=args.get("sets"), wishes=args.get("wishes", ""),
                                       markets=args.get("markets"), g1=args.get("g1"), g2=args.get("g2"),
                                       llm="fake", render="fake")  # fmt: skip
        except (ServiceError, ValueError, KeyError) as exc:
            return _error(f"{type(exc).__name__}: {exc}")
        self.index.add_run(result["run_id"], p.owner, args.get("collection", ""), p.channel, result["state"])
        out = {"content": [{"type": "text", "text": ""}], "structuredContent": result}
        self._refresh(result["run_id"])
        self._decorate(out, result["run_id"])
        return out


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
