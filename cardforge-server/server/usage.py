# ruff: noqa: E501
"""The one source of usage events (`usage_events` of the index): every event is attributed to a
**verified** owner — the owner of the personal key (MCP), of the browser session (web) or of the
run (worker). Fields: owner, channel `web | mcp | worker`, operation, run, cost, model, time.

Why from day one: an AI Portal service built from the m3-editor template wrote no usage at all
(`[M3-USAGE-MISSING]`, AGENTS.md 2026-11-10); the future portal service hands these events to the
portal's `usage-logger` (`cost_usd_override`) instead of recomputing them.

- MCP: one event per `tools/call` (`tool:<name>`), written by `server.tools.ServerTools.call`.
- web: one event per gate action and download (`web:g1/approve`, `web:g2/decisions`, `web:delivery.zip`,
  `web:login`). **Not** recorded: static files, page renders, run images (no user operation).
- worker: one event per paid OpenRouter call of a run, with the actual cost the core settled in
  the run's `costs.json` (`render`, `judge:<id>`, `llm:set_sheet_review`, …); idempotent by
  `<run>:<call id>`, so re-syncing never doubles a call. Free calls (cost 0) and calls still
  reserved (no actual cost yet) are not events; they are picked up once settled.
"""

from __future__ import annotations

from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace, judge_profile, render_profile
from server.store_index import Index


def record(index: Index, owner: str, channel: str, operation: str, *, run_id: str | None = None, ok: bool = True) -> None:
    index.record_usage(owner=owner, channel=channel, operation=operation, run_id=run_id, ok=ok)


def _model(ws: Workspace, store: RunStore, call: dict) -> tuple[str | None, str | None]:
    m = store.manifest()
    kind = call.get("kind") or ""
    try:
        if kind.startswith("judge:"):
            jp = judge_profile(ws, kind.split(":", 1)[1])
            return jp.model, jp.route or jp.provider
        if kind in ("render", "edit") or kind.startswith("render"):
            vid = call.get("variant_id")
            name = (m.variant_profiles.get(vid) if vid else None) or m.render_profile
            if name:
                p = render_profile(ws, name)
                return p.model_id, p.backend
    except (ValueError, KeyError):
        pass
    return None, None


def sync_worker(index: Index, ws: Workspace, run_id: str) -> int:
    """Settled paid calls of the run's ledger → worker events (new ones only). Returns how many."""
    store = RunStore(ws.run_dir(run_id))
    owner = index.owner(run_id)
    if owner is None or not store.exists("costs.json"):
        return 0
    added = 0
    for call in store.read_json("costs.json").get("calls", []):
        cost = call.get("actual_usd")
        if cost is None or float(cost) <= 0:
            continue
        model, provider = _model(ws, store, call)
        added += index.record_usage(
            owner=owner, channel="worker", operation=call.get("kind") or "call", run_id=run_id,
            cost_usd=float(cost), model=model, provider=provider, ts=call.get("reserved_at"),
            source_key=f"{run_id}:{call['id']}",
        )  # fmt: skip
    return added


def unsettled(ws: Workspace, run_id: str, month: str) -> float:
    """Reservations of this month without an actual cost yet (counted at their estimate)."""
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("costs.json"):
        return 0.0
    return sum(float(c.get("estimate_usd") or 0) for c in store.read_json("costs.json").get("calls", [])
               if c.get("actual_usd") is None and str(c.get("reserved_at", ""))[:7] == month)  # fmt: skip
