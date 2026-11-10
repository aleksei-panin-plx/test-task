# ruff: noqa: E501
"""User decisions 2026-11-10 for the server: attributed usage events from day one (web | mcp |
worker, `[M3-USAGE-MISSING]`), monthly limit $50 per user changed by the admin, runs kept 30 days
after delivery. Offline: FakeBackend with a fake per-image price, no network, $0."""

from __future__ import annotations

import time

from cardforge.orchestrator.store import RunStore
from helpers import client
from server import budget_users, retention, usage
from server.settings import FOLDER, Settings


def _priced_run(app, c, collection="Лето", sets=1, price=0.05):
    c.init()
    rid = c.tool("start_run", collection=collection, sets=sets)["run_id"]
    RunStore(app.ws.run_dir(rid)).update_manifest(
        lambda m: m.runtime_options.update(fake_backend={"cost_per_image_usd": price}))  # fmt: skip
    return rid


def test_mcp_call_and_worker_call_each_give_one_attributed_event(make_app):
    app = make_app()
    c = client(app, "anna")
    c.init()
    assert app.index.usage() == []  # initialize / tools/list are protocol, not operations
    c.tool("list_runs")
    ev = app.index.usage()
    assert len(ev) == 1 and (ev[0]["owner"], ev[0]["channel"], ev[0]["operation"]) == ("anna", "mcp", "tool:list_runs")
    rid = c.tool("start_run", collection="Лето", sets=1)["run_id"]
    # one paid call of the worker, with its actual cost, as the core settles it in costs.json
    store = RunStore(app.ws.run_dir(rid))
    store.write("costs.json", {"max_cost_usd": 1.0, "calls": [
        {"id": "c1", "kind": "render", "card_id": "v01_c01", "variant_id": "v01", "estimate_usd": 0.08,
         "actual_usd": 0.049, "reserved_at": "2026-11-10T10:00:00+00:00"},
        {"id": "c2", "kind": "llm:plan", "estimate_usd": 0.0, "actual_usd": 0.0, "reserved_at": "2026-11-10T10:00:00+00:00"},
        {"id": "c3", "kind": "render", "estimate_usd": 0.08, "actual_usd": None, "reserved_at": "2026-11-10T10:00:01+00:00"},
    ]})  # fmt: skip
    assert usage.sync_worker(app.index, app.ws, rid) == 1
    assert usage.sync_worker(app.index, app.ws, rid) == 0  # idempotent: never doubled
    w = app.index.usage(channel="worker")
    assert len(w) == 1 and w[0]["owner"] == "anna" and w[0]["run_id"] == rid
    assert w[0]["cost_usd"] == 0.049 and w[0]["operation"] == "render" and w[0]["ts"].startswith("2026-11-10")
    assert w[0]["model"] == "fake-backend-v1"
    # every event belongs to a verified owner and a known channel
    assert {e["channel"] for e in app.index.usage()} == {"mcp", "worker"}


def test_web_actions_are_events_static_and_pages_are_not(make_app):
    app = make_app(worker_mode="inline")
    c = client(app, "anna")
    rid = _priced_run(app, c)
    c.login(c.key)
    before = len(app.index.usage(channel="web"))
    assert before == 1  # web:login
    for path in (f"/web/runs/{rid}/g1", "/web/", "/web/static/g1.js", "/web/static/g2-core.js"):
        assert c.http("GET", path, headers={"Authorization": ""})[0] == 200
    assert len(app.index.usage(channel="web")) == before  # pages and static: no events
    status, _, body = c.web_post(f"/web/runs/{rid}/g1/approve", {})
    assert status == 200, body
    web = app.index.usage(channel="web")
    assert [e["operation"] for e in web] == ["web:login", "web:approve_plan"] and all(e["owner"] == "anna" for e in web)
    assert app.dispatcher.wait_idle(120)
    c.web_post(f"/web/runs/{rid}/g3/export", {})
    c.http("GET", f"/web/runs/{rid}/delivery.zip", headers={"Authorization": ""})
    assert [e["operation"] for e in app.index.usage(channel="web")][-2:] == ["web:export_run", "web:delivery.zip"]
    # the worker's paid calls of this run: one event each, with the fake price as the actual cost
    calls = [x for x in RunStore(app.ws.run_dir(rid)).read_json("costs.json")["calls"] if (x["actual_usd"] or 0) > 0]
    w = app.index.usage(channel="worker")
    assert len(w) == len(calls) >= 10 and all(e["owner"] == "anna" and e["run_id"] == rid for e in w)


def test_default_limit_is_50_and_the_admin_changes_it(make_app, monkeypatch):
    monkeypatch.delenv("USER_MONTH_LIMIT_USD", raising=False)
    assert Settings.from_env().user_month_limit_usd == 50.0
    monkeypatch.setenv("USER_MONTH_LIMIT_USD", "75")
    assert Settings.from_env().user_month_limit_usd == 75.0
    app = make_app()
    admin = client(app, "root", ("admin",))
    assert budget_users.limit_of(app.index, app.s, "anna") == 50.0
    assert admin.http("POST", "/admin/limits", {"owner": "anna", "usd": 80})[0] == 200
    assert budget_users.limit_of(app.index, app.s, "anna") == 80.0
    assert client(app, "anna").http("POST", "/admin/limits", {"owner": "anna", "usd": 999})[0] == 403
    assert admin.http("POST", "/admin/limits", {"owner": "anna", "usd": None})[0] == 200
    assert budget_users.limit_of(app.index, app.s, "anna") == 50.0


def test_monthly_limit_stops_the_run_as_partial_budget_exhausted(make_app):
    app = make_app(worker_mode="inline")
    admin = client(app, "root", ("admin",))
    admin.http("POST", "/admin/limits", {"owner": "anna", "usd": 0.12})
    c = client(app, "anna")
    rid = _priced_run(app, c, price=0.05)
    c.tool("approve_plan", run_id=rid, reviewer="x", human_answer="Запускай")
    assert app.dispatcher.wait_idle(120)
    v = c.tool("get_run", run_id=rid)
    assert v["state"] == "partial" and v["reason"] == "budget_exhausted"
    assert v["message"] == budget_users.LIMIT_MESSAGE and "$" not in v["message"]  # no amounts for the designer
    spent = budget_users.month_spent(app.index, app.ws, "anna")
    assert 0 < spent <= 0.12 + 1e-9
    status, _, body = admin.http("GET", "/admin/costs")
    assert status == 200 and '"anna"' in body.decode()
    # the admin raises the limit, the person says «продолжи»: the run finishes
    admin.http("POST", "/admin/limits", {"owner": "anna", "usd": 50})
    c.tool("resume_run", run_id=rid)
    assert app.dispatcher.wait_idle(120)
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")
    assert RunStore(app.ws.run_dir(rid)).manifest().status_reason != "budget_exhausted"


def test_runs_are_archived_30_days_after_delivery(make_app):
    app = make_app(worker_mode="inline")
    assert app.s.retention_days == 30
    c = client(app, "anna")
    rid = _priced_run(app, c, price=0.0)
    c.tool("approve_plan", run_id=rid, reviewer="x", human_answer="Запускай")
    assert app.dispatcher.wait_idle(120)
    other = _priced_run(app, c, "Путешествие", price=0.0)  # never delivered: never archived
    assert retention.due(app.index, app.ws, 30) == []  # not delivered yet
    c.tool("export_run", run_id=rid)
    day = 86400
    assert retention.due(app.index, app.ws, 30, now=time.time() + 29 * day) == []
    assert retention.due(app.index, app.ws, 30, now=time.time() + 31 * day) == [rid]
    path = retention.archive(app.index, app.ws, rid)
    assert path.exists() and not app.ws.run_dir(rid).exists() and app.ws.run_dir(other).exists()
    res = c.call("get_run", run_id=rid)
    assert res["isError"] and "в архиве" in res["content"][0]["text"]
    retention.restore(app.index, app.ws, rid)
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")
    assert FOLDER.exists()
