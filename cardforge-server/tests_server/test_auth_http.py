# ruff: noqa: E501
"""Personal keys: required, revocable, expiring, scoped; Origin check; browser sessions die with
the key; G3 can be limited to the browser (portal plan §4)."""

from __future__ import annotations

from dataclasses import replace

from helpers import Client, client
from server.auth import issue, revoke


def test_key_required_revoked_expired_and_scoped(make_app):
    app = make_app()
    assert Client(app.s.public_url, None).rpc("tools/list")[0] == 401
    assert Client(app.s.public_url, "cf_bad_key").rpc("tools/list")[0] == 401
    cid, key = issue(app.index, "anna", ("read",))
    reader = Client(app.s.public_url, key)
    reader.init()
    res = reader.call("start_run", collection="Лето", sets=1)
    assert res["isError"] and "run" in res["content"][0]["text"]
    assert not reader.call("list_runs").get("isError")
    revoke(app.index, cid)
    assert reader.rpc("tools/list")[0] == 401
    _, old = issue(app.index, "anna", ("read",), days=-1)
    assert Client(app.s.public_url, old).rpc("tools/list")[0] == 401
    # session from another key is refused
    c = client(app, "anna")
    c.init()
    other = client(app, "boris")
    other.session = c.session
    assert other.rpc("tools/list")[0] == 404
    # DNS rebinding rule
    status, _, _ = c.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "http://evil.example"})
    assert status == 403
    assert c.http("GET", "/mcp")[0] == 405
    assert c.rpc("tools/call", {"name": "nope", "arguments": {}})[2]["result"]["isError"]


def test_browser_session_follows_the_key(make_app):
    app = make_app()
    cid, key = issue(app.index, "anna")
    c = Client(app.s.public_url, None)
    status, h, _ = c.http("GET", "/web/")
    assert status == 303 and h["Location"].startswith("/web/login")
    c.login(key)
    status, h, page = c.http("GET", "/web/")
    assert status == 200 and "Мои прогоны".encode() in page
    revoke(app.index, cid)
    assert c.http("GET", "/web/")[0] == 303  # the session dies with the key
    bad = c.http("POST", "/web/login", raw=b"key=cf_x_y&next=/web/", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert bad[0] == 401


def test_g3_only_from_the_browser_when_configured(make_app):
    app = make_app()
    s = replace(app.s, g3_channels=("web",))
    app.s = app.tools.s = s
    c = client(app, "anna")
    c.init()
    rid = c.tool("start_run", collection="Лето", sets=1)["run_id"]
    c.tool("approve_plan", run_id=rid, reviewer="x", human_answer="Запускай")
    assert app.dispatcher.wait_idle(240)
    res = c.call("export_run", run_id=rid)
    assert res["isError"] and "/g3" in res["content"][0]["text"]
    c.login(c.key)
    status, _, body = c.web_post(f"/web/runs/{rid}/g3/export", {})
    assert status == 200, body
