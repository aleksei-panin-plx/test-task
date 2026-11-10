# ruff: noqa: E501
"""Server tests: a real HTTP server on a free port, offline (FakeBackend, fake planner)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from server import core_hooks
from server.app import App
from server.auth import issue
from server.settings import Settings

FOLDER = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, base: str, key: str | None):
        self.base = base
        self.key = key
        self.session: str | None = None
        self.cookie: str | None = None
        self.n = 0

    def http(self, method: str, path: str, body=None, headers: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        h = {"Content-Type": "application/json", **(headers or {})}
        if self.key:
            h.setdefault("Authorization", f"Bearer {self.key}")
        if self.cookie:
            h["Cookie"] = f"cf_session={self.cookie}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=120) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def rpc(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        status, h, body = self.http("POST", "/mcp", msg, headers)
        if method == "initialize" and status == 200:
            self.session = h.get("Mcp-Session-Id")
        return status, h, (json.loads(body) if body else None)

    def init(self):
        status, _, body = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                  "clientInfo": {"name": "pytest", "version": "0"}})  # fmt: skip
        assert status == 200 and body["result"]["serverInfo"]["name"] == "cardforge-server"
        assert self.rpc("notifications/initialized", notify=True)[0] == 202
        return body["result"]

    def call(self, name: str, **args) -> dict:
        status, _, body = self.rpc("tools/call", {"name": name, "arguments": args})
        assert status == 200, body
        return body["result"]

    def tool(self, name: str, **args) -> dict:
        result = self.call(name, **args)
        assert not result.get("isError"), result["content"][0]["text"]
        return result["structuredContent"]

    def login(self, key: str) -> None:
        raw = f"key={key}&next=/web/".encode()
        status, h, _ = self.http("POST", "/web/login", raw=raw, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                                          "Authorization": ""})  # fmt: skip
        assert status == 303, status
        assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
        self.cookie = h["Set-Cookie"].split("cf_session=", 1)[1].split(";", 1)[0]

    def web_post(self, path: str, body: dict):
        return self.http("POST", path, body, {"X-CardForge": "1", "Origin": self.base, "Authorization": ""})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CARDFORGE_STOP", raising=False)
    monkeypatch.delenv("CARDFORGE_WORKER", raising=False)
    apps: list[App] = []

    def make(**kw) -> App:
        s = Settings(home=FOLDER, runs_dir=tmp_path / "runs", db_path=tmp_path / "runs/_server/index.sqlite3",
                     port=0, demo=kw.pop("demo", True), worker="external",
                     worker_mode=kw.pop("worker_mode", "process"), worker_poll_s=0.05, **kw)  # fmt: skip
        app = App(s)
        app.serve(background=True)
        url = f"http://127.0.0.1:{app.httpd.server_port}"
        s2 = replace(s, public_url=url, allowed_origins=(url,))
        app.s = app.tools.s = app.dispatcher.s = s2
        apps.append(app)
        return app

    yield make
    for a in apps:
        a.shutdown()
    core_hooks.uninstall()


def client(app: App, owner: str = "anna", scopes=("read", "run", "decide", "export")) -> Client:
    _, key = issue(app.index, owner, scopes)
    return Client(app.s.public_url, key)
