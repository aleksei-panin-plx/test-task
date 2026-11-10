# ruff: noqa: E501
"""Monthly OpenRouter limit per user (user 2026-11-10: **$50 by default, changed by the admin**).

Default: `USER_MONTH_LIMIT_USD` (50). Per user: `python -m server.budget_users set --owner anna --usd 80`
(or `POST /admin/limits`), `unset` returns to the default, `show` lists limits and this month's spend.

How it is enforced without touching the core: before every job of a run the dispatcher sets the
run's own ceiling (the core `request.budget.max_cost_usd`, journaled by `service.set_budget`) to

    already spent by this run + (limit − this month's spend of the owner)

capped by the OpenRouter ledger remainder for paid runs. The core reserves every paid call against
that ceiling before sending it; a call that does not fit is not sent and the run ends as
`partial / budget_exhausted` — exactly the core's behaviour. Month spend = settled worker usage
events of the month + this month's unsettled reservations of the owner's runs (at their estimate).
Two runs of one user running at the same moment can each use the remainder computed at their job
start: the overrun is bounded by what the other run spends during that job.
The designer is told only that the monthly limit is reached (no amounts, user 2026-11-10).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from cardforge.contracts.request import CollectionRequest
from cardforge.orchestrator import product, service
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace
from server import usage
from server.settings import Settings
from server.store_index import Index

LIMIT_MESSAGE = "месячный лимит генерации исчерпан — прогон остановлен; продолжить можно после увеличения лимита администратором или в следующем месяце (resume_run)"


def month_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def limit_of(index: Index, settings: Settings, owner: str) -> float:
    own = index.limit(owner)
    return settings.user_month_limit_usd if own is None else own


def month_spent(index: Index, ws: Workspace, owner: str, month: str | None = None) -> float:
    month = month or month_now()
    live = [r["run_id"] for r in index.runs(owner) if r["state"] != "archived"]
    for rid in live:
        usage.sync_worker(index, ws, rid)
    return index.worker_cost(owner, month) + sum(usage.unsettled(ws, rid, month) for rid in live)


def apply_cap(index: Index, settings: Settings, ws: Workspace, run_id: str) -> dict:
    """Set the run's ceiling from the owner's monthly remainder (before a job starts)."""
    owner = index.owner(run_id)
    store = RunStore(ws.run_dir(run_id))
    request = store.read_model("request.json", CollectionRequest)
    committed = BudgetLedger(store, request.budget.max_cost_usd).committed()
    limit = limit_of(index, settings, owner)
    remaining = max(0.0, limit - month_spent(index, ws, owner))
    cap = committed + remaining
    if product._paid(ws, store):
        cap = min(cap, committed + max(0.0, product._ledger_room(ws.root)))
    cap = round(cap, 6)
    if abs(cap - request.budget.max_cost_usd) > 1e-9:
        service.set_budget(ws, run_id, cap, f"server: monthly limit of {owner} (${limit:.2f}), remainder ${remaining:.2f}")
    return {"owner": owner, "limit_usd": limit, "remaining_usd": round(remaining, 6), "run_cap_usd": cap}


def limit_reached(store: RunStore) -> bool:
    """The run stopped on its ceiling after a monthly cap was applied by the server."""
    m = store.manifest()
    if m.status_reason != "budget_exhausted":
        return False
    return any(e.type == "budget_warning" and "monthly limit" in str(e.payload.get("reason", ""))
               for e in store.events())  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description="CardForge server: monthly limit per user")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("--owner", required=True)
    s.add_argument("--usd", type=float, required=True)
    u = sub.add_parser("unset")
    u.add_argument("--owner", required=True)
    sub.add_parser("show")
    a = ap.parse_args()
    st = Settings.from_env()
    index = Index(st.db_path)
    if a.cmd == "set":
        if a.usd < 0:
            raise SystemExit("limit must be >= 0")
        index.set_limit(a.owner, a.usd, "cli")
    elif a.cmd == "unset":
        index.clear_limit(a.owner)
    base = Workspace.from_env(st.home)
    ws = Workspace(root=base.root, runs_dir=st.runs_dir, refs_dir=base.refs_dir)
    owners = sorted({r["owner"] for r in index.runs()} | {r["owner"] for r in index.limits()})
    json.dump({"default_usd": st.user_month_limit_usd, "month": month_now(),
               "users": {o: {"limit_usd": limit_of(index, st, o), "own_limit": index.limit(o) is not None,
                             "spent_usd": round(month_spent(index, ws, o), 6)} for o in owners}},
              sys.stdout, ensure_ascii=False, indent=1)  # fmt: skip
    print()


if __name__ == "__main__":
    main()
