"""MCP Streamable HTTP transport (spec 2025-11-25 / 2025-06-18 / 2025-03-26), tools only, no SDK.

`POST /mcp` takes one JSON-RPC message and answers with one `application/json` body (the spec
allows JSON instead of an SSE stream; every tool returns at once, heavy work is queued and
polled with `get_run`). Notifications get `202 Accepted`. `GET /mcp` (server-initiated SSE) is
not offered: `405`. `DELETE /mcp` ends the session. Auth: `Authorization: Bearer cf_…` on every
request; `Origin` is checked against `CARDFORGE_ALLOWED_ORIGINS` (DNS-rebinding rule).
"""

from __future__ import annotations

import json
import secrets
import sys

from cardforge.mcp.server import INSTRUCTIONS, PROTOCOLS
from server import SERVER_VERSION
from server.auth import Principal
from server.tools import ServerTools, describe

SERVER_INSTRUCTIONS = (
    INSTRUCTIONS
    + " Сервер: все инструменты отвечают сразу, рендер идёт в очереди сервера — опрашивай get_run; "
    "страницы гейтов для человека — ссылки pages в ответе get_run (вход по личному ключу)."
)


class Sessions:
    def __init__(self):
        self.ids: dict[str, str] = {}  # session id → credential id

    def open(self, p: Principal) -> str:
        sid = secrets.token_urlsafe(18)
        self.ids[sid] = p.credential_id
        return sid

    def valid(self, sid: str | None, p: Principal) -> bool:
        return sid is None or self.ids.get(sid) == p.credential_id

    def close(self, sid: str | None) -> None:
        self.ids.pop(sid or "", None)


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_post(tools: ServerTools, sessions: Sessions, p: Principal, raw: bytes,
                session_id: str | None) -> tuple[int, dict, dict | None]:  # fmt: skip
    """(HTTP status, extra headers, JSON body or None)."""
    try:
        msg = json.loads(raw or b"null")
    except ValueError:
        return 400, {}, _err(None, -32700, "parse error")
    if isinstance(msg, list):
        return 400, {}, _err(None, -32600, "batches are not supported")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return 400, {}, _err(None, -32600, "invalid request")
    method, mid = msg.get("method"), msg.get("id")
    if method is None:  # a response to a server request: none are sent
        return 202, {}, None
    if mid is None:
        return 202, {}, None  # notifications/initialized, cancelled …
    if method != "initialize" and not sessions.valid(session_id, p):
        return 404, {}, _err(mid, -32001, "unknown session")
    params = msg.get("params") or {}
    headers: dict = {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cardforge-server", "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }
        headers["Mcp-Session-Id"] = sessions.open(p)
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": describe()}
    elif method == "tools/call":
        try:
            result = tools.call(p, params.get("name", ""), params.get("arguments") or {})
        except Exception as exc:  # never drop the connection: report as a JSON-RPC error
            print(f"mcp tools/call: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 200, {}, _err(mid, -32603, f"{type(exc).__name__}: {exc}")
    else:
        return 200, {}, _err(mid, -32601, f"unknown method {method}")
    return 200, headers, {"jsonrpc": "2.0", "id": mid, "result": result}
