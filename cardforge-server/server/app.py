# ruff: noqa: E501  (routes and messages)
"""CardForge server: one HTTP process (stdlib `ThreadingHTTPServer`, no extra dependencies).

Routes
- `POST|DELETE /mcp`, `GET /mcp` → 405 — MCP Streamable HTTP (`server.mcp_http`), Bearer key.
- `GET /health` — server and core version, queue, kill switch, key presence (never the key).
- `/web/login`, `/web/logout`, `/web/` — browser session from a personal key; own runs.
- `/web/runs/<run>/g1|g2|g3` — gate pages; `POST …/g1/approve|g1/edit|g2/decisions|g3/regenerate|g3/export`
  (session cookie + `X-CardForge: 1` + same Origin); `GET …/file/<path>` (images of the run),
  `GET …/delivery.zip`.
- `GET /admin/costs|limits|usage`, `POST /admin/limits|stop` — scope `admin` (spend, monthly limit per
  user, attributed usage events, kill switch).

    python -m server.app            # CARDFORGE_SERVER_HOST / _PORT, WORKER_CONCURRENCY, …
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import threading
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import cardforge
from cardforge.orchestrator import product
from cardforge.orchestrator.execution import stop_requested
from cardforge.orchestrator.service import ServiceError
from cardforge.orchestrator.store import RunStore, now
from cardforge.orchestrator.workspace import Workspace
from server import SERVER_VERSION, auth, budget_users, usage, web
from server.core_hooks import acting_as, install
from server.mcp_http import Sessions, handle_post
from server.settings import Settings
from server.store_index import Index
from server.tools import ServerTools, _locks
from server.worker import Dispatcher, refresh

MAX_BODY = 10 * 1024 * 1024
RUN_ROUTE = re.compile(r"^/web/runs/([A-Za-z0-9._-]+)/(g1|g2|g3|file/.+|delivery\.zip|g1/approve|g1/edit|g2/decisions|g3/regenerate|g3/export)$")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class App:
    def __init__(self, settings: Settings):
        self.s = settings
        self.index = Index(settings.db_path)
        install(self.index)
        base = Workspace.from_env(settings.home)
        self.ws = Workspace(root=base.root, runs_dir=settings.runs_dir, refs_dir=base.refs_dir)
        self.ws.runs_dir.mkdir(parents=True, exist_ok=True)
        self.tools = ServerTools(settings, self.index, self.ws)
        self.sessions = Sessions()
        self.dispatcher = Dispatcher(settings, self.index, self.ws)
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ lifecycle

    def serve(self, *, background: bool = False) -> ThreadingHTTPServer:
        app = self

        class Handler(_Handler):
            pass

        Handler.app = app
        self.httpd = ThreadingHTTPServer((self.s.host, self.s.port), Handler)
        self.httpd.daemon_threads = True
        if self.s.worker == "embedded":
            self.dispatcher.start_background()
        if background:
            threading.Thread(target=self.httpd.serve_forever, name="cardforge-http", daemon=True).start()
        else:
            print(f"cardforge-server {SERVER_VERSION} (core {cardforge.__version__}) on {self.s.host}:{self.httpd.server_port}", flush=True)
            try:
                self.httpd.serve_forever()
            finally:
                self.dispatcher.stop()
        return self.httpd

    def shutdown(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.dispatcher.stop()

    def health(self) -> dict:
        from dotenv import dotenv_values

        key = bool(os.environ.get("OPENROUTER_API_KEY") or dotenv_values(self.ws.root / ".env", interpolate=False).get("OPENROUTER_API_KEY"))
        jobs = self.index.jobs()
        usage = shutil.disk_usage(self.ws.runs_dir)
        return {
            "ok": True, "server_version": SERVER_VERSION, "core_version": cardforge.__version__,
            "kill_switch": stop_requested(self.ws.runs_dir), "openrouter_key_present": key, "demo": self.s.demo,
            "queue": {s: sum(j["status"] == s for j in jobs) for s in ("queued", "running")},
            "worker": {"mode": self.s.worker, "concurrency": self.s.worker_concurrency},
            "runs_free_gb": round(usage.free / 1e9, 1),
        }  # fmt: skip


class _Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "cardforge-server"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # no keys or cookies in logs: path and status only
        sys.stderr.write(f"{self.address_string()} {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    # ------------------------------------------------------------ plumbing

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json; charset=utf-8", headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", web.CSP)
            self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data, headers: dict | None = None) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), headers=headers)

    def _html(self, text: str, status: int = 200, headers: dict | None = None) -> None:
        self._send(status, text.encode(), "text/html; charset=utf-8", headers)

    def _redirect(self, to: str, headers: dict | None = None) -> None:
        self._send(303, b"", "text/plain", {"Location": to, **(headers or {})})

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        return self.rfile.read(n) if n else b""

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin.rstrip("/") in self.app.s.allowed_origins or origin in (
            f"http://{self.headers.get('Host')}", f"https://{self.headers.get('Host')}")

    def _cookie(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["cf_session"].value if "cf_session" in c else None

    def _web_principal(self) -> auth.Principal | None:
        return auth.session(self.app.index, self._cookie())

    # ------------------------------------------------------------ verbs

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, verb: str) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/mcp":
                return self._mcp(verb)
            if path == "/health" and verb == "GET":
                return self._json(200, self.app.health())
            if path.startswith("/admin/"):
                return self._admin(verb, path)
            if path.startswith("/web/static/") and verb == "GET":
                found = web.static(path.removeprefix("/web/static/"))
                return self._send(200, *found) if found else self._json(404, {"error": "not found"})
            if path.startswith("/web"):
                return self._web(verb, path)
            return self._json(404, {"error": "not found"})
        except (ServiceError, ValueError, KeyError) as exc:
            return self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # keep serving
            print(f"http {verb} {path}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return self._json(500, {"error": "internal error"})

    # ------------------------------------------------------------ MCP

    def _mcp(self, verb: str) -> None:
        if not self._origin_ok():
            return self._json(403, {"error": "origin not allowed"})
        p = auth.verify(self.app.index, auth.bearer(self.headers.get("Authorization")), "mcp")
        if p is None:
            return self._json(401, {"error": "personal key required (Authorization: Bearer cf_…)"},
                              {"WWW-Authenticate": 'Bearer realm="cardforge"'})
        if verb == "GET":
            return self._send(405, b"", "text/plain", {"Allow": "POST, DELETE"})
        if verb == "DELETE":
            self.app.sessions.close(self.headers.get("Mcp-Session-Id"))
            return self._send(204, b"", "text/plain")
        status, headers, body = handle_post(self.app.tools, self.app.sessions, p, self._body(), self.headers.get("Mcp-Session-Id"))
        if body is None:
            return self._send(status, b"", "text/plain", headers)
        return self._json(status, body, headers)

    # ------------------------------------------------------------ admin

    def _admin(self, verb: str, path: str) -> None:
        p = auth.verify(self.app.index, auth.bearer(self.headers.get("Authorization")), "admin")
        if p is None or not p.can("admin"):
            return self._json(403, {"error": "admin key required"})
        if path == "/admin/costs" and verb == "GET":
            for r in self.app.index.runs():
                refresh(self.app.index, self.app.ws, r["run_id"])
            month = parse_qs(urlsplit(self.path).query).get("month", [None])[0] or budget_users.month_now()
            owners = sorted({r["owner"] for r in self.app.index.runs()} | {r["owner"] for r in self.app.index.limits()})
            return self._json(200, {"month": month, "default_limit_usd": self.app.s.user_month_limit_usd, "by_owner": {
                o: {"spent_usd": round(budget_users.month_spent(self.app.index, self.app.ws, o, month), 6),
                    "limit_usd": budget_users.limit_of(self.app.index, self.app.s, o),
                    "worker_calls": len(self.app.index.usage(o, "worker", month))} for o in owners}})  # fmt: skip
        if path == "/admin/limits" and verb == "GET":
            return self._json(200, {"default_usd": self.app.s.user_month_limit_usd, "users": self.app.index.limits()})
        if path == "/admin/limits" and verb == "POST":
            data = json.loads(self._body() or b"{}")
            owner = str(data.get("owner") or "").strip()
            if not owner:
                return self._json(400, {"error": "owner required"})
            if data.get("usd") is None:
                self.app.index.clear_limit(owner)
            else:
                if float(data["usd"]) < 0:
                    return self._json(400, {"error": "usd must be >= 0"})
                self.app.index.set_limit(owner, float(data["usd"]), p.owner)
            return self._json(200, {"owner": owner, "limit_usd": budget_users.limit_of(self.app.index, self.app.s, owner)})
        if path == "/admin/usage" and verb == "GET":
            q = parse_qs(urlsplit(self.path).query)
            return self._json(200, {"events": self.app.index.usage(q.get("owner", [None])[0], q.get("channel", [None])[0],
                                                                   q.get("month", [None])[0])})
        if path == "/admin/stop" and verb == "POST":
            data = json.loads(self._body() or b"{}")
            stop = self.app.ws.runs_dir / "STOP"
            if data.get("on", True):
                stop.write_text(f"admin {p.owner} {now()}\n", encoding="utf-8")
            else:
                stop.unlink(missing_ok=True)
            return self._json(200, {"kill_switch": stop.exists()})
        return self._json(404, {"error": "not found"})

    # ------------------------------------------------------------ web

    def _web(self, verb: str, path: str) -> None:
        app = self.app
        if path == "/web/login":
            if verb == "GET":
                nxt = parse_qs(urlsplit(self.path).query).get("next", ["/web/"])[0]
                return self._html(web.login(next_url=nxt))
            if not self._origin_ok():
                return self._json(403, {"error": "origin not allowed"})
            form = parse_qs(self._body().decode())
            p = auth.verify(app.index, (form.get("key") or [""])[0].strip(), "web")
            nxt = (form.get("next") or ["/web/"])[0]
            nxt = nxt if nxt.startswith("/web/") and "//" not in nxt else "/web/"
            if p is None:
                return self._html(web.login("ключ не принят (неверный, отозван или истёк)", nxt), 401)
            token = auth.open_session(app.index, p, app.s.session_hours)
            usage.record(app.index, p.owner, "web", "web:login")
            secure = "; Secure" if app.s.public_url.startswith("https://") else ""
            return self._redirect(nxt, {"Set-Cookie": f"cf_session={token}; HttpOnly; SameSite=Strict; Path=/{secure}; Max-Age={int(app.s.session_hours * 3600)}"})
        if path == "/web/logout":
            auth.close_session(app.index, self._cookie())
            return self._redirect("/web/login", {"Set-Cookie": "cf_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        p = self._web_principal()
        if p is None:
            if verb == "GET":
                return self._redirect(f"/web/login?next={path}")
            return self._json(401, {"error": "войдите заново"})
        if path in ("/web", "/web/") and verb == "GET":
            for r in app.index.runs(p.owner):
                refresh(app.index, app.ws, r["run_id"])
            return self._html(web.runs_list(p.owner, app.index.runs(p.owner)))
        m = RUN_ROUTE.match(path)
        if not m:
            return self._json(404, {"error": "not found"})
        rid, what = m.groups()
        if app.index.owner(rid) != p.owner:
            return self._json(404, {"error": f"нет прогона {rid}"})
        if app.index.state(rid) == "archived":
            return self._json(410, {"error": f"прогон {rid} в архиве (хранение {app.s.retention_days:g} дней после выдачи)"})
        if verb == "POST":
            if not self._origin_ok() or self.headers.get("X-CardForge") != "1":
                return self._json(403, {"error": "csrf check failed"})
            return self._web_action(p, rid, what, json.loads(self._body() or b"{}"))
        if verb != "GET":
            return self._json(405, {"error": "method not allowed"})
        store = RunStore(app.ws.run_dir(rid))
        if what == "g1":
            return self._html(web.g1(rid, p.owner, product.plan_view(app.ws, rid), product.view(app.ws, rid)))
        if what == "g2":
            if store.manifest().state != "awaiting_g2" or not store.exists("review/g2.json"):
                v = product.view(app.ws, rid)
                return self._html(web._page("G2", f"<h1>G2</h1><p class='note'>Сейчас на G2 ничего не ждёт (состояние {web.e(v['state'])}, дальше: {web.e(v['next'])}). По умолчанию G2 выключен (G2 = none).</p>", [], rid, p.owner, "g2"))
            with _locks[rid]:
                return self._html(web.g2(rid, p.owner, store, app.ws.root))
        if what == "g3":
            v = product.view(app.ws, rid)
            if v["state"] not in ("completed", "partial", "awaiting_g2"):
                return self._html(web._page("G3", f"<h1>G3</h1><p class='note'>Листы появятся после рендера (состояние {web.e(v['state'])}, дальше: {web.e(v['next'])}).</p>", [], rid, p.owner, "g3"))
            with _locks[rid]:
                sheets = product.sheets(app.ws, rid)
            return self._html(web.g3(rid, p.owner, sheets, v, store.exists("delivery/current/index.html")))
        if what == "delivery.zip":
            usage.record(app.index, p.owner, "web", "web:delivery.zip", run_id=rid)
            return self._delivery(store)
        if what.startswith("file/"):
            return self._file(store, what.removeprefix("file/"))
        return self._json(404, {"error": "not found"})

    def _web_action(self, p: auth.Principal, rid: str, what: str, data: dict) -> None:
        tools = self.app.tools
        calls = {
            "g1/approve": ("approve_plan", {"human_answer": str(data.get("human_answer") or "Запускай (страница G1)")}),
            "g1/edit": ("edit_plan", {"edit": str(data.get("edit") or "")}),
            "g3/regenerate": ("regenerate_cards", {"cards": list(data.get("cards") or []), "note": str(data.get("note") or "")}),
            "g3/export": ("export_run", {}),
        }  # fmt: skip
        if what == "g2/decisions":
            return self._g2_decisions(p, rid, data)
        if what not in calls:
            return self._json(404, {"error": "not found"})
        name, args = calls[what]
        out = tools.call(p, name, {"run_id": rid, "reviewer": p.owner, **args})
        if out.get("isError"):
            return self._json(400, {"error": out["content"][0]["text"]})
        sc = out.get("structuredContent") or {}
        return self._json(200, {"message": sc.get("message") or f"записано; дальше: {sc.get('next', '')}", "state": sc.get("state")})

    def _g2_decisions(self, p: auth.Principal, rid: str, data: dict) -> None:
        if not p.can("decide"):
            return self._json(403, {"error": "у ключа нет права «decide»"})
        if data.get("format") != "cardforge-g2-review-v2" or not data.get("page_id"):
            return self._json(400, {"error": "ожидается JSON страницы G2 (cardforge-g2-review-v2, слепой)"})
        store = RunStore(self.app.ws.run_dir(rid))
        data = {**data, "reviewer": p.owner}
        rel = f"review/g2-web-{now().replace(':', '')}.json"
        with _locks[rid], acting_as(p.owner, "web"):
            store.write(rel, data)
            v = product.decide_cards(self.app.ws, rid, p.owner, from_file=store.path(rel))
        self.app.index.record_decision(rid, "G2", p.owner, "web", "g2_page", {"file": rel, "decisions": data.get("decisions", {})})
        usage.record(self.app.index, p.owner, "web", "web:g2/decisions", run_id=rid)
        refresh(self.app.index, self.app.ws, rid)
        return self._json(200, {"message": f"дальше: {v.get('next')}", "state": v.get("state")})

    def _file(self, store: RunStore, rel: str) -> None:
        path = (store.root / rel).resolve()
        if (not path.is_relative_to(store.root.resolve()) or ".private" in path.name
                or path.suffix.lower() not in IMAGE_TYPES or not path.is_file()):  # fmt: skip
            return self._json(404, {"error": "not found"})
        return self._send(200, path.read_bytes(), IMAGE_TYPES[path.suffix.lower()])

    def _delivery(self, store: RunStore) -> None:
        base = store.path("delivery/current")
        if not (base / "index.html").exists():
            return self._json(404, {"error": "выдачи ещё нет: «Принять и собрать выдачу» на G3"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(base.rglob("*")):
                if f.is_file() and ".private" not in f.name:
                    z.write(f, Path(store.run_id) / f.relative_to(base))
        return self._send(200, buf.getvalue(), "application/zip",
                          {"Content-Disposition": f'attachment; filename="{store.run_id}-delivery.zip"'})


def main() -> None:
    App(Settings.from_env()).serve()


if __name__ == "__main__":
    main()
