# ruff: noqa: E501  (tool descriptions and Russian messages)
"""MCP over stdio (newline-delimited JSON-RPC 2.0), tools only, no third-party SDK.

Stage 7 local plugin transport: Claude Code / Codex start `cardforge mcp` and talk on stdin/stdout.
Logs go to stderr only (stdout is the protocol). The server variant reuses `cardforge.mcp.tools`.
"""

from __future__ import annotations

import json
import sys
from typing import IO

import cardforge
from cardforge.mcp import tools
from cardforge.orchestrator.workspace import Workspace

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "CardForge рисует наборы карточек-предметов в стиле игры. Сценарий — скилл cardforge (SKILL.md): "
    "тема → план (G1, всегда показывать) → фоновый рендер → флаговые карты (G2) → выбор варианта (G3) → "
    "выдача. Решения человека — его словами. Не писать промпты рендера, не снимать block политик, "
    "не говорить о стоимости, ничего не публиковать."
)


def handle(ws: Workspace, msg: dict) -> dict | None:
    """One JSON-RPC message → response (None for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None  # notifications/initialized, cancelled, …
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cardforge", "version": cardforge.__version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools.describe()}
    elif method == "tools/call":
        result = tools.call(ws, params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_stdio(ws: Workspace, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(ws, msg)
            except Exception as exc:  # never kill the session: report and go on
                print(f"cardforge mcp: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}  # fmt: skip
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
