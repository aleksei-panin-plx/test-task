# ruff: noqa: E501
"""e2e over MCP Streamable HTTP on FakeBackend: collection plan (G1) → run in the queue →
G3 (page and MCP) → delivery; the run opens with the core CLI unchanged. Offline, $0."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile

from cardforge.mcp import tools as core_tools
from cardforge.orchestrator.store import RunStore
from helpers import FOLDER, client


def test_collection_plan_run_g3_delivery_over_http(make_app):
    app = make_app()
    c = client(app, "anna")
    info = c.init()
    assert "get_run" in info["instructions"]
    status, _, body = c.rpc("tools/list")
    names = [t["name"] for t in body["result"]["tools"]]
    assert names == [t.name for t in core_tools.TOOLS]  # the local plugin's tool set, unchanged
    assert len(names) == 18

    # G1: collection plan, two sets named in the request
    v = c.tool("start_run", collection="Лето", sets=2, wishes="побольше солнца")
    rid = v["run_id"]
    assert v["state"] == "awaiting_g1" and v["next"] == "show_plan"
    assert "cost_usd" not in v and v["pages"]["g1"].endswith(f"/web/runs/{rid}/g1")
    plan = c.tool("get_plan", run_id=rid)
    assert plan["brief"]["plan_mode"] == "collection" and len(plan["variants"]) == 2
    assert all(len(s["cards"]) == 10 for s in plan["variants"])

    # «Запускай»: answers at once, the render waits in the server queue
    v = c.tool("approve_plan", run_id=rid, reviewer="кто-то другой", human_answer="Запускай")
    assert v["queue"]["status"] == "queued" and v["next"] == "wait_render"
    store = RunStore(app.ws.run_dir(rid))
    g1 = [e.payload for e in store.events() if e.type == "human_decision" and e.payload.get("gate") == "G1"]
    assert g1 and all(p.get("reviewer") == "anna" for p in g1)  # bound to the key's owner
    m = store.manifest()
    assert (m.owner, m.deployment, m.client) == ("anna", "server", "mcp")
    assert m.versions["server_version"] and m.versions["core_version"]

    assert app.dispatcher.wait_idle(240)
    job = app.index.job(rid)
    assert job["status"] == "done" and job["pid"]  # a separate `cardforge work` process
    v = c.tool("get_run", run_id=rid)
    assert v["state"] in ("completed", "partial") and v["next"] == "export"
    assert v["progress"]["total"] == 20

    # G3 sheets in the chat (images attached), then the page
    res = c.call("get_sheets", run_id=rid)
    assert [x["type"] for x in res["content"]].count("image") == 2
    assert len(res["structuredContent"]["sets"]) == 2
    c.login(c.key)
    status, h, page = c.http("GET", f"/web/runs/{rid}/g3", headers={"Authorization": ""})
    assert status == 200 and "script-src 'self'" in h["Content-Security-Policy"]
    assert b"<script>" not in page and b"/file/sheets/" in page
    status, h, img = c.http("GET", f"/web/runs/{rid}/file/sheets/v01.png", headers={"Authorization": ""})
    assert status == 200 and img[:4] == b"\x89PNG"
    assert c.http("GET", f"/web/runs/{rid}/file/../../../pyproject.toml", headers={"Authorization": ""})[0] == 404

    # G3 «Принять и собрать выдачу» on the page → delivery; zip download
    status, _, body = c.web_post(f"/web/runs/{rid}/g3/export", {})
    assert status == 200, body
    assert store.exists("delivery/current/index.html")
    status, _, data = c.http("GET", f"/web/runs/{rid}/delivery.zip", headers={"Authorization": ""})
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert status == 200 and any(n.endswith("index.html") for n in names) and any(n.endswith(".png") for n in names)
    assert not any(".private" in n for n in names)
    assert c.tool("get_run", run_id=rid)["next"] == "review_collection"
    # the same G3 action over MCP (default channels mcp,web, like the local plugin)
    assert "screened_out" in c.tool("export_run", run_id=rid)
    decisions = app.index.decisions(rid)
    assert {d["gate"] for d in decisions} >= {"G1", "G3"} and {d["channel"] for d in decisions} == {"mcp", "web"}

    # the core CLI opens the server run without conversion (portal plan §9)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(FOLDER / "src"), str(FOLDER)])}
    for cmd in ("status", "export"):
        out = subprocess.run([sys.executable, "-m", "cardforge", cmd, rid, "--root", str(FOLDER),
                              "--runs-dir", str(app.ws.runs_dir)], capture_output=True, text=True, env=env, timeout=300)  # fmt: skip
        assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(subprocess.run([sys.executable, "-m", "cardforge", "status", rid, "--root", str(FOLDER),
                                      "--runs-dir", str(app.ws.runs_dir)], capture_output=True, text=True,
                                     env=env, timeout=300).stdout)["state"] in ("completed", "partial")  # fmt: skip


def test_g2_page_from_the_core_page_posts_blind_decisions(make_app):
    """G2 on (per-run `g2=flagged`): the core page with its script external, decisions posted."""
    app = make_app(worker_mode="inline")
    c = client(app, "boris")
    c.init()
    rid = c.tool("start_run", collection="Кино", sets=1, g2="flagged")["run_id"]
    c.tool("approve_plan", run_id=rid, reviewer="x", human_answer="Запускай")
    assert app.dispatcher.wait_idle(120)
    v = c.tool("get_run", run_id=rid)
    assert v["state"] == "awaiting_g2" and v["next"] == "show_flags"
    assert c.tool("open_review_page", run_id=rid)["page"].endswith(f"/web/runs/{rid}/g2")
    assert c.call("submit_card_decisions", run_id=rid, reviewer="x", from_file="/etc/passwd")["isError"]
    c.login(c.key)
    status, h, page = c.http("GET", f"/web/runs/{rid}/g2", headers={"Authorization": ""})
    text = page.decode()
    assert status == 200 and "<script>" not in text and 'id="cf-data"' in text
    assert "Отправить решения" in text and "v01_c01" not in text  # blind: no card ids
    status, _, js = c.http("GET", "/web/static/g2-core.js", headers={"Authorization": ""})
    assert status == 200 and b"JSON.parse(document.getElementById" in js and b"__DATA__" not in js
    data = json.loads(text.split('id="cf-data" type="application/json">', 1)[1].split("</script>", 1)[0])
    cells = [cell["id"] for row in data["rows"] for cell in row["cells"] if cell["decidable"]]
    payload = {"format": "cardforge-g2-review-v2", "gate": "g2", "blind": True, "page_id": data["page_id"],
               "reviewer": "не тот", "decisions": {cid: "accept" for cid in cells}, "notes": {}, "reasons": {}}
    assert c.http("POST", f"/web/runs/{rid}/g2/decisions", payload, {"Authorization": ""})[0] == 403  # no CSRF header
    status, _, body = c.web_post(f"/web/runs/{rid}/g2/decisions", payload)
    assert status == 200, body
    store = RunStore(app.ws.run_dir(rid))
    g2 = [e.payload for e in store.events() if e.type == "human_decision" and e.payload.get("gate") in ("G2", "g2")]
    assert g2 and all(p.get("reviewer") == "boris" for p in g2)
    assert app.dispatcher.wait_idle(120)
    assert c.tool("get_run", run_id=rid)["state"] in ("completed", "partial")
