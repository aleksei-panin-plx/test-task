from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # The person's own CardForge settings must never leak into the tests.
    for name in ("CARDFORGE_LITE_HOME", "CARDFORGE_LITE_RUNS_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ws(tmp_path) -> Workspace:
    """The real knowledge, policies and prompts; runs in a temporary folder."""
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs")


@pytest.fixture
def root_copy(tmp_path) -> Path:
    """An editable copy of the knowledge, policies, prompts and config (for edits of knowledge)."""
    dst = tmp_path / "root"
    for rel in ("knowledge", "policies", "prompts", "config"):
        shutil.copytree(ROOT / rel, dst / rel)
    return dst
