# ruff: noqa: E501
"""The plugin: MCP over stdio, manifests, launcher, installer dry run, Light scope by imports."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys

from cardforge.llm.fake import FakeLLM
from cardforge.mcp import server

from .conftest import ROOT

TOOLS = {"doctor", "start_plan", "get_agent_tasks", "submit_agent_answer", "resume_run", "get_plan",
         "edit_plan", "approve_plan", "get_run", "list_runs"}  # fmt: skip


def _rpc(ws, method, params=None, mid=1):
    return server.handle(ws, {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def _call(ws, name, **args):
    res = _rpc(ws, "tools/call", {"name": name, "arguments": args})["result"]
    assert not res.get("isError"), res["content"][0]["text"]
    return res["structuredContent"]


def test_initialize_and_tools(ws):
    init = _rpc(ws, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["serverInfo"]["name"] == "cardforge-lite" and init["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in _rpc(ws, "tools/list")["result"]["tools"]}
    assert names == TOOLS


def test_mcp_flow_to_tables(ws):
    """start_plan → tasks → answers → get_plan, as Claude Code / Codex would do it."""
    fake = FakeLLM()
    out = _call(ws, "start_plan", collection="Лето", sets=2)
    rid = out["run_id"]
    while out["next"] == "answer_tasks":
        for t in _call(ws, "get_agent_tasks", run_id=rid)["tasks"]:
            _call(ws, "submit_agent_answer", run_id=rid, task=t["task"], answer=getattr(fake, "_" + t["step"])(t),
                  provider="claude-code", model="test")  # fmt: skip
        out = _call(ws, "resume_run", run_id=rid)
    assert out["next"] == "show_plan"
    plan = _call(ws, "get_plan", run_id=rid)
    assert "## Сет 2." in plan["markdown"] and plan["files"]["csv"].endswith("tables/cards.csv")
    assert _call(ws, "approve_plan", run_id=rid, reviewer="Аня", human_answer="ок")["next"] == "done"


def test_refusal_is_an_error_text(ws):
    res = _rpc(ws, "tools/call", {"name": "start_plan", "arguments": {"collection": ""}})["result"]
    assert res["isError"] and "тема коллекции" in res["content"][0]["text"]


def test_stdio_transport(ws):
    out = io.StringIO()
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "not json",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]  # fmt: skip
    server.serve_stdio(ws, io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0]["result"] == {} and replies[1]["error"]["code"] == -32700 and len(replies) == 2


def test_doctor_tool(ws):
    out = _call(ws, "doctor")
    assert out["ok"] and out["edition"] == "lite"


def test_manifests_and_launcher():
    claude = json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text())
    codex = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    agents = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert claude["name"] == codex["name"] == market["plugins"][0]["name"] == agents["plugins"][0]["name"] == "cardforge-lite"
    mcp = json.loads((ROOT / "plugin/.mcp.json").read_text())["mcpServers"]["cardforge-lite"]
    assert mcp["command"].endswith("/bin/cardforge-lite-mcp")
    launcher = ROOT / "plugin/bin/cardforge-lite-mcp"
    assert os.access(launcher, os.X_OK)
    assert (ROOT / "plugin/skills/cardforge-lite/SKILL.md").exists()
    assert {p.stem for p in (ROOT / "plugin/commands").glob("*.md")} == {"new", "edit", "status", "doctor"}
    # The skill names only tools that exist.
    skill = (ROOT / "plugin/skills/cardforge-lite/SKILL.md").read_text(encoding="utf-8")
    for name in TOOLS - {"list_runs", "get_run"}:
        assert f"`{name}" in skill, name


def test_launcher_starts_the_server(tmp_path):
    """The launcher finds the core next to the plugin and answers `initialize` on stdio."""
    if not (ROOT / ".venv/bin/cardforge").exists():
        return  # no installed environment in this folder
    env = {**os.environ, "HOME": str(tmp_path), "CARDFORGE_LITE_RUNS_DIR": str(tmp_path / "runs")}
    env.pop("CARDFORGE_LITE_HOME", None)
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    out = subprocess.run([str(ROOT / "plugin/bin/cardforge-lite-mcp")], input=msg, capture_output=True,
                         text=True, env=env, timeout=60)  # fmt: skip
    assert json.loads(out.stdout.splitlines()[0])["result"]["serverInfo"]["name"] == "cardforge-lite"


def test_installer_dry_run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("claude", "uv"):
        (bindir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bindir / tool).chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "CARDFORGE_AGENT_SEARCH": "path"}
    out = subprocess.run(["sh", str(ROOT / "installer/install.sh"), "--dry-run", "--home", str(tmp_path / "cf")],
                         capture_output=True, text=True, env=env, timeout=60)  # fmt: skip
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "uv sync --frozen --no-dev" in text
    assert "plugin install cardforge-lite@cardforge-lite" in text
    assert "fetch-assets" not in text and "OPENROUTER" not in text and "data-parts" not in text


def test_installer_needs_an_agent(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CARDFORGE_AGENT_SEARCH": "path"}
    out = subprocess.run(["sh", str(ROOT / "installer/install.sh"), "--dry-run", "--home", str(tmp_path / "cf")],
                         capture_output=True, text=True, env=env, timeout=60)  # fmt: skip
    assert out.returncode == 1 and "Claude Code" in out.stderr


# ---------------------------------------------------------------- Light scope, checked by imports

FORBIDDEN = ("render", "evaluate", "export", "prompting", "lora", "ui", "autonomy", "assets",
             "openrouter", "smoke", "service", "product", "execution")  # fmt: skip
ENTRY = ("cardforge.cli", "cardforge.mcp.server", "cardforge.doctor")


def _imports(module: str) -> set[str]:
    path = ROOT / "src" / (module.replace(".", "/") + ".py")
    if not path.exists():
        path = path.with_suffix("") / "__init__.py"
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("cardforge"):
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names
                         if (ROOT / "src" / node.module.replace(".", "/") / f"{a.name}.py").exists())  # fmt: skip
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("cardforge"))
    return found


def test_every_module_is_used_and_nothing_else_is_shipped():
    seen, todo = set(), list(ENTRY)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        todo += sorted(_imports(mod) - seen)
    files = {".".join(p.relative_to(ROOT / "src").with_suffix("").parts)
             for p in (ROOT / "src").rglob("*.py")}  # fmt: skip
    # Package __init__ files are loaded with their modules; `__main__` is `python -m cardforge`.
    modules = {f for f in files if not f.endswith(".__init__")} - {"cardforge.__main__"}
    assert modules - seen == set(), "modules no entry point imports"
    assert not [m for m in modules if any(part in FORBIDDEN for part in m.split(".")[1:])]


def test_no_heavy_dependencies():
    import tomllib

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert {d.split(">")[0].split("=")[0] for d in deps} == {"pydantic", "typer", "pyyaml"}
    assert "PIL" not in sys.modules and "numpy" not in sys.modules
