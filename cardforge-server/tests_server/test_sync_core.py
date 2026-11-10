# ruff: noqa: E501
"""`scripts/sync_core.py`: diff report, sha256 in docs/core-sync.md, divergences kept, private
data never copied or deleted; the folder's own core matches its last sync record."""

from __future__ import annotations

import importlib.util
import inspect
import re
import subprocess
import sys
from datetime import UTC, datetime

from cardforge.mcp import tools as core_tools
from cardforge.orchestrator import product
from helpers import FOLDER
from server import core_hooks, tools

spec = importlib.util.spec_from_file_location("sync_core", FOLDER / "scripts/sync_core.py")
sync_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_core)


def _core(root, version="1"):
    (root / "src/cardforge").mkdir(parents=True)
    (root / "src/cardforge/__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "config").mkdir()
    (root / "config/runtime.toml").write_text('[deployment.server]\nllm_providers = ["fake"]\n')
    (root / "knowledge/frames").mkdir(parents=True)
    (root / "knowledge/a.json").write_text("{}")
    (root / "knowledge/x.private.json").write_text('{"label": "secret"}')
    (root / "knowledge/labels.private").mkdir()
    (root / "knowledge/labels.private/r1.json").write_text("{}")
    (root / "tests").mkdir()
    (root / "tests/test_a.py").write_text("def test_a(): pass\n")


PATCH = '''[[patch]]
path = "config/runtime.toml"
why = "server"
old = 'llm_providers = ["fake"]'
new = 'llm_providers = ["host-agent", "fake"]'
'''


def test_sync_reports_diff_applies_divergences_and_never_moves_private_data(tmp_path):
    src, dst = tmp_path / "local", tmp_path / "server"
    _core(src)
    dst.mkdir()
    (dst / "core-sync.toml").write_text('keep = ["prompts/server-only.toml"]\n' + PATCH)
    (dst / "prompts").mkdir()
    (dst / "prompts/server-only.toml").write_text("x = 1\n")
    (dst / "knowledge/frames").mkdir(parents=True)
    (dst / "knowledge/frames/occlusion_v2.png").write_bytes(b"data-asset")  # unpacked customer data
    (dst / "knowledge/stale.json").write_text("{}")
    r = sync_core.sync(src, dst, now=datetime(2026, 11, 10, tzinfo=UTC))
    assert r["ok"] and "knowledge/a.json" in r["added"] and r["removed"] == ["knowledge/stale.json"]
    assert (dst / "config/runtime.toml").read_text().count("host-agent") == 1
    assert not (dst / "knowledge/x.private.json").exists() and not (dst / "knowledge/labels.private").exists()
    assert (dst / "knowledge/frames/occlusion_v2.png").read_bytes() == b"data-asset"
    assert (dst / "prompts/server-only.toml").exists()
    report = (dst / "docs/core-sync.md").read_text()
    assert r["source_core_sha256"] in report and r["target_core_sha256"] in report and "2026-11-10" in report
    assert "| `config/runtime.toml` | server | applied |" in report
    # a second sync: nothing changes, the first one moves to the history
    r2 = sync_core.sync(src, dst)
    assert r2["ok"] and not (r2["added"] or r2["changed"] or r2["removed"])
    assert "- 2026-11-10T00:00:00+00:00: ядро" in (dst / "docs/core-sync.md").read_text()
    # back to the local copy: the server divergence is reversed, server-only files stay out
    back = tmp_path / "local2"
    _core(back)
    r3 = sync_core.sync(dst, back)
    assert r3["ok"] and 'llm_providers = ["fake"]' in (back / "config/runtime.toml").read_text()
    assert not (back / "prompts/server-only.toml").exists()


def test_a_divergence_that_no_longer_applies_fails_the_sync(tmp_path):
    src, dst = tmp_path / "local", tmp_path / "server"
    _core(src)
    (src / "config/runtime.toml").write_text('[deployment.server]\nllm_providers = ["fake", "other"]\n')
    dst.mkdir()
    (dst / "core-sync.toml").write_text(PATCH)
    r = sync_core.sync(src, dst)
    assert not r["ok"] and r["patches"][0]["status"] == "FAILED"
    assert not (dst / "config").exists()  # nothing written
    out = subprocess.run([sys.executable, "-I", str(FOLDER / "scripts/sync_core.py"), str(src), str(dst)],
                         capture_output=True, text=True)  # fmt: skip
    assert out.returncode == 2


def test_this_folder_matches_its_last_core_sync_record():
    report = (FOLDER / "docs/core-sync.md").read_text(encoding="utf-8")
    recorded = re.search(r"после синхронизации и правок: `([0-9a-f]{64})`", report).group(1)
    assert sync_core.core_sha256(FOLDER) == recorded, "src/cardforge/ changed outside sync_core.py"
    runtime = (FOLDER / "config/runtime.toml").read_text(encoding="utf-8")
    assert 'llm_providers = ["host-agent", "fake"]  # server copy' in runtime


def test_core_hook_points_exist_and_the_tool_set_is_the_plugins():
    """The two core attributes server/core_hooks.py replaces still exist with the same shape."""
    create = inspect.signature(core_hooks._ORIGINAL["create_run"]).parameters
    assert {"owner", "deployment", "client"} <= set(create)
    assert list(inspect.signature(core_hooks._ORIGINAL["start_worker"]).parameters) == ["ws", "run_id"]
    assert "_start_worker(ws, run_id)" in inspect.getsource(product.resume)
    assert "service.create_run(" in inspect.getsource(product.start)
    assert {t.name for t in core_tools.TOOLS} == set(tools.SCOPE)  # every core tool has a scope
