# ruff: noqa: E501
"""Two users at once: own runs only, separate run folders and cost lines, a FIFO queue limited by
WORKER_CONCURRENCY; the kill switch `runs/STOP`, a run's STOP and the paid lock."""

from __future__ import annotations

from contextlib import contextmanager

from cardforge.orchestrator import product
from cardforge.orchestrator.store import RunStore
from helpers import client


def _approved(app, c, collection):
    c.init()
    rid = c.tool("start_run", collection=collection, sets=1)["run_id"]
    c.tool("approve_plan", run_id=rid, reviewer="x", human_answer="Запускай")
    return rid


def test_two_users_are_isolated_and_the_second_waits_in_the_queue(make_app):
    app = make_app(worker_concurrency=1)
    a, b = client(app, "anna"), client(app, "boris")
    ra, rb = _approved(app, a, "Лето"), _approved(app, b, "Путешествие")
    assert ra != rb and app.ws.run_dir(ra) != app.ws.run_dir(rb)
    app.dispatcher.tick()  # concurrency 1: the first starts, the second waits
    assert app.index.job(ra)["status"] == "running" and app.index.job(rb)["status"] == "queued"
    assert b.tool("get_run", run_id=rb)["queue"] == {"status": "queued", "position": 1}
    # another owner's run does not exist for a key
    assert b.call("get_run", run_id=ra)["isError"] and a.call("get_plan", run_id=rb)["isError"]
    assert b.call("stop_run", run_id=ra)["isError"]
    assert [r["run_id"] for r in a.tool("list_runs")["runs"]] == [ra]
    assert [r["run_id"] for r in b.tool("list_runs")["runs"]] == [rb]
    assert app.dispatcher.wait_idle(240)
    for c, rid, owner in ((a, ra, "anna"), (b, rb, "boris")):
        assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")
        m = RunStore(app.ws.run_dir(rid)).manifest()
        assert m.owner == owner and m.deployment == "server"
    costs = app.index.costs_by_owner()
    assert set(costs) == {"anna", "boris"} and all(v["runs"] == 1 for v in costs.values())
    # cost is accounted per owner, but never shown to the designer
    assert "cost_usd" not in a.tool("get_run", run_id=ra)


def test_kill_switch_and_run_stop(make_app):
    app = make_app()
    c = client(app, "anna")
    rid = _approved(app, c, "Лето")
    (app.ws.runs_dir / "STOP").write_text("test\n")
    app.dispatcher.tick()
    assert app.index.job(rid)["status"] == "queued"  # nothing starts while runs/STOP exists
    assert app.health()["kill_switch"] is True
    (app.ws.runs_dir / "STOP").unlink()
    c.tool("stop_run", run_id=rid)  # the run's own STOP cancels its queued job
    app.dispatcher.tick()
    assert app.index.job(rid)["status"] == "cancelled"
    assert c.tool("get_run", run_id=rid)["next"] == "stopped"
    c.tool("resume_run", run_id=rid)  # «Продолжи» clears the run STOP and queues it again
    assert app.dispatcher.wait_idle(240)
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")


def test_admin_stop_and_costs_need_the_admin_scope(make_app):
    app = make_app()
    user, admin = client(app, "anna"), client(app, "root", ("admin",))
    assert user.http("GET", "/admin/costs")[0] == 403
    assert admin.http("POST", "/admin/stop", {"on": True})[0] == 200
    assert (app.ws.runs_dir / "STOP").exists()
    assert admin.http("POST", "/admin/stop", {"on": False})[0] == 200
    assert not (app.ws.runs_dir / "STOP").exists()
    status, _, body = admin.http("GET", "/admin/costs")
    assert status == 200 and "default_limit_usd" in body.decode()


def test_paid_runs_go_through_the_core_paid_lock(make_app, monkeypatch):
    """The worker runs `product.work`, which takes `runs/_api/paid-run.lock` for a paid run
    (here the run is made «paid» by patching `_paid`; nothing is sent anywhere)."""
    import cardforge.render.edit_runtime as edit_runtime

    taken = []

    @contextmanager
    def recorder(path, **kw):
        taken.append(path)
        yield

    monkeypatch.setattr(edit_runtime, "paid_lock", recorder)
    monkeypatch.setattr(product, "_paid", lambda ws, store: True)
    app = make_app(worker_mode="inline")
    c = client(app, "anna")
    rid = _approved(app, c, "Лето")
    assert app.dispatcher.wait_idle(120)
    assert taken == [app.ws.root / "runs/_api/paid-run.lock"]
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")


def test_process_jobs_are_separate_pids_and_a_lost_job_is_requeued(make_app):
    app = make_app()
    c = client(app, "anna")
    rid = _approved(app, c, "Лето")
    job = app.index.claim("dead-dispatcher")  # a dispatcher that died after claiming
    app.index.set_pid(job["id"], 999_999_9)
    assert app.dispatcher.recover() == [job["id"]]
    assert app.dispatcher.wait_idle(240)
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")
