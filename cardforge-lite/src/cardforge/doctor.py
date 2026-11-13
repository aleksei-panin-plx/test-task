# ruff: noqa: E501
"""Read-only diagnostics of CardForge Light; never contacts a network service.

Light plans only (G1): the plan is written by the host agent (Claude Code / Codex) on its own
subscription, so there is no OpenRouter key, ledger or image route to check.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

REQUIRED = (
    "knowledge/set_grammar/v1.yaml",
    "knowledge/catalog/vocabulary.yaml",
    "knowledge/catalog/cards.jsonl",
    "knowledge/catalog/human_review.jsonl",
    "knowledge/catalog/set_statistics.json",
    "knowledge/style_profile/reference_cards_v1.yaml",
    "knowledge/style_profile/human_feedback_v1.json",
    "config/material_rules.toml",
    "config/material_budget.toml",
    "policies/ip/baseline.md",
    "prompts/planner/collection.md",
    "prompts/planner/card_rules.md",
    "prompts/planner/normalize.md",
    "prompts/planner/repair.md",
    "prompts/planner/human_edits.md",
)


class Check(BaseModel):
    name: str
    status: Literal["ok", "warning", "error"]
    detail: str


class DoctorReport(BaseModel):
    schema_version: int = 1
    edition: str = "lite"
    observed_at: str
    platform: str
    python: str
    versions: dict[str, str | None]
    mode: str = "plan-only (G1)"
    checks: list[Check] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _knowledge(root: Path) -> list[Check]:
    checks = []
    missing = [rel for rel in REQUIRED if not (root / rel).is_file()]
    checks.append(Check(name="files", status="error" if missing else "ok",
                        detail=f"нет файлов: {', '.join(missing)}" if missing else f"{len(REQUIRED)} файлов знаний, политик и инструкции на месте"))  # fmt: skip
    if missing:
        return checks
    # The real loaders: a broken YAML, vocabulary or prompt shows up here, not in the first plan.
    try:
        from cardforge.planner import planner
        from cardforge.planner.collection import layout
        from cardforge.planner.validators import load_grammar, load_vocabulary

        grammar, vocab = load_grammar(root), load_vocabulary(root)
        for key in ("motif", "color", "materials", "category"):
            if not vocab.get(key):
                raise ValueError(f"vocabulary.yaml: нет раздела {key}")
        motifs = grammar.get("category_motifs", {})
        bad = sorted({m for k, ms in motifs.items() if k.isdigit() for m in ms} - set(vocab["motif"]))
        if bad:
            raise ValueError(f"set_grammar category_motifs: мотивов нет в словаре: {bad}")
        layout(root, 16)
        planner.few_shot(root)
        planner.style_digest(root)
        for name in ("collection", "card_rules", "normalize", "repair", "human_edits"):
            if not planner.prompt(root, name):
                raise ValueError(f"prompts/planner/{name}.md пуст")
        checks.append(Check(name="knowledge", status="ok", detail=f"грамматика {grammar.get('version')}, словарь {vocab.get('version')}, инструкция планировщика читается"))
    except Exception as exc:  # report, never crash the doctor
        checks.append(Check(name="knowledge", status="error", detail=f"{type(exc).__name__}: {exc}"))
    try:
        from cardforge.policy.loader import lint

        report = lint(root)
        errors = report.get("issues", [])
        checks.append(Check(name="policies", status="error" if errors else "ok",
                            detail="; ".join(errors[:5]) if errors else f"пакетов политик: {len(report.get('packs', []))}"))  # fmt: skip
    except Exception as exc:
        checks.append(Check(name="policies", status="error", detail=f"{type(exc).__name__}: {exc}"))
    return checks


def collect(root: Path) -> DoctorReport:
    root = Path(root).resolve()
    checks: list[Check] = []
    py_ok = sys.version_info[:2] == (3, 12)
    checks.append(Check(name="python", status="ok" if py_ok else "warning", detail=platform.python_version()))
    checks += _knowledge(root)
    runs = Path(os.environ.get("CARDFORGE_LITE_RUNS_DIR") or root / "runs")
    try:
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=runs):
            pass
        checks.append(Check(name="runs", status="ok", detail=f"планы пишутся в {runs}"))
    except OSError as exc:
        checks.append(Check(name="runs", status="error", detail=f"нет записи в {runs}: {exc}"))
    checks.append(Check(name="planner", status="ok",
                        detail="план пишет агент (Claude Code или Codex) по задачам ядра; ключи и платные сервисы не нужны"))  # fmt: skip
    return DoctorReport(
        observed_at=datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
        platform=f"{platform.system()} {platform.machine()}",
        python=platform.python_version(),
        versions={n: package_version(n) for n in ("cardforge-lite", "pydantic", "typer", "pyyaml")},
        checks=checks,
    )
