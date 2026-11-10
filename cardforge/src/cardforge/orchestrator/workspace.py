"""Working folders and runtime profiles (TA §13.4 rules 3 and 5).

Knowledge, policies, prompts and config live under `root`; runs and reference images can
move elsewhere through `CARDFORGE_RUNS_DIR` / `CARDFORGE_REFS_DIR`, so a server worker
does not build paths from one shared project root.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from cardforge.evaluate.card_qa import JudgeProfile
from cardforge.prompting.assemble import RenderProfile

RUNTIME_CONFIG = "config/runtime.toml"


@dataclass(frozen=True)
class Workspace:
    root: Path
    runs_dir: Path
    refs_dir: Path

    @classmethod
    def from_env(cls, root: Path | None = None) -> Workspace:
        root = Path(os.environ.get("CARDFORGE_HOME") or root or ".")
        main = tomllib.loads((root / "config/cardforge.toml").read_text(encoding="utf-8"))
        refs = main.get("defaults", {}).get("references_dir", "dataset")
        return cls(
            root=root,
            runs_dir=Path(os.environ.get("CARDFORGE_RUNS_DIR") or root / "runs"),
            # Kept relative when inside the project so bundle hashes do not depend on the host.
            refs_dir=Path(os.environ.get("CARDFORGE_REFS_DIR") or refs),
        )

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def refs_path(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    def config(self) -> dict:
        return tomllib.loads((self.root / RUNTIME_CONFIG).read_text(encoding="utf-8"))


def _known(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def render_profile(ws: Workspace, name: str) -> RenderProfile:
    profiles = ws.config()["render_profiles"]
    if name not in profiles:
        raise ValueError(f"unknown render profile {name!r}; known: {sorted(profiles)}")
    return RenderProfile(id=name, **_known(RenderProfile, profiles[name]))


def judge_profile(ws: Workspace, name: str) -> JudgeProfile:
    judges = ws.config()["judges"]
    if name not in judges:
        raise ValueError(f"unknown judge profile {name!r}; known: {sorted(judges)}")
    data = dict(judges[name])
    data["admitted"] = tuple(data.get("admitted", ()))
    data["enabled_in_config"] = data.pop("enabled", True)
    data["fake"] = dict(data.get("fake", {}))
    return JudgeProfile(id=name, **_known(JudgeProfile, data))
