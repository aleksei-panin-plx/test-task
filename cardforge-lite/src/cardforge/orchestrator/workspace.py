# ruff: noqa: E501
"""Working folders (TA §13.4): knowledge, policies, prompts and config under `root`; runs under
`runs/` or `CARDFORGE_LITE_RUNS_DIR`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path
    runs_dir: Path

    @classmethod
    def from_env(cls, root: Path | None = None) -> Workspace:
        root = Path(os.environ.get("CARDFORGE_LITE_HOME") or root or ".")
        return cls(root=root, runs_dir=Path(os.environ.get("CARDFORGE_LITE_RUNS_DIR") or root / "runs"))

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id
