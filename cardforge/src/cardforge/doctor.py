# ruff: noqa: E501
"""Read-only diagnostics; never import MLX or contact a paid provider."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import psutil
from dotenv import dotenv_values
from pydantic import BaseModel, Field

GB = 1_000_000_000
GIB = 1024**3
CODEX_FALLBACKS = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)


class Check(BaseModel):
    name: str
    status: Literal["ok", "warning", "error"]
    detail: str


class DoctorReport(BaseModel):
    schema_version: int = 1
    observed_at: str
    platform: str
    architecture: str
    python: str
    versions: dict[str, str | None]
    credentials: dict[str, bool]
    disk_free_gb: float
    memory_gib: float | None
    codex_path: str | None
    codex_auth_file_present: bool
    codex_access_verified: bool = False
    mode: str
    llm_provider: str
    render_profile: str | None
    render_access_verified: bool = False
    api_budget_usd: float
    paid_api_enabled: bool
    checks: list[Check] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(c.status == "error" for c in self.checks)


def find_codex() -> Path | None:
    found = shutil.which("codex")
    if found:
        return Path(found)
    return next((p for p in CODEX_FALLBACKS if p.is_file() and os.access(p, os.X_OK)), None)


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def select_mode(
    *,
    codex_present: bool,
    codex_auth_present: bool,
    local_lora_present: bool,
    gemini_present: bool,
    paid_api_enabled: bool,
    local_only: bool = False,
    local_base_present: bool = False,
) -> tuple[str, str, str | None]:
    if local_only:
        if local_base_present:
            return "keyless", "host-agent", "local-klein4b"
        return "plan-only", "host-agent", None
    if codex_present and codex_auth_present:
        return "keyless", "host-agent", "closed-gpt-codex"
    if local_lora_present:
        return "keyless", "host-agent", "local-klein4b-lora"
    if gemini_present and paid_api_enabled:
        return "developer-api", "gemini", "closed-nano"
    return "plan-only", "host-agent", None


def collect(root: Path, probe: bool | None = None) -> DoctorReport:
    """`probe`: send the OpenRouter route probes (paid, ≈ $0); None → `[doctor] route_probe`."""
    root = root.resolve()
    config_path = root / "config/cardforge.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    dev = config.get("development", {})
    thresholds = config.get("doctor", {})
    # Deliberately load only the project .env and never log its values.
    dotenv = dotenv_values(root / ".env", interpolate=False)
    credentials = {
        key: bool(os.environ.get(key) or dotenv.get(key))
        for key in ("GEMINI_API_KEY", "HF_TOKEN", "OPENROUTER_API_KEY")
    }
    versions = {
        name: package_version(name)
        for name in (
            "cardforge",
            "pydantic",
            "typer",
            "mflux",
            "mlx",
            "google-genai",
            "uv",
        )
    }
    bootstrap_uv = root / ".bootstrap/bin/uv"
    uv_path = shutil.which("uv") or (str(bootstrap_uv) if bootstrap_uv.is_file() else None)
    if uv_path:
        try:
            probe = subprocess.run(
                [uv_path, "--version"], capture_output=True, text=True, timeout=5
            )
            if probe.returncode == 0:
                versions["uv"] = probe.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    codex = find_codex()
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    auth_present = (codex_home / "auth.json").is_file()
    budget = max(0.0, float(dev.get("api_budget_usd", 0)))
    paid_enabled = bool(dev.get("paid_api_enabled", False)) and budget > 0
    checks: list[Check] = []
    free = shutil.disk_usage(root).free / GB
    try:
        memory = psutil.virtual_memory().total / GIB
    except (OSError, RuntimeError):
        memory = None
    is_apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    # A raw base model is insufficient: the configured LoRA must exist too.
    local_config = config.get("local", {})
    weights = local_config.get("klein4b_weights")
    lora = local_config.get("klein4b_lora")
    local_ready = bool(
        is_apple
        and memory is not None
        and memory >= 24
        and versions["mflux"]
        and versions["mlx"]
        and weights
        and lora
        and (root / weights).is_dir()
        and (root / lora).is_file()
    )
    mode, llm, render = select_mode(
        codex_present=codex is not None,
        codex_auth_present=auth_present,
        local_lora_present=local_ready,
        gemini_present=credentials["GEMINI_API_KEY"],
        paid_api_enabled=paid_enabled,
        local_only=bool(dev.get("local_generation_only", False)),
        local_base_present=bool(
            is_apple
            and memory is not None
            and memory >= 24
            and versions["mflux"]
            and versions["mlx"]
            and weights
            and (root / weights / "mflux_model.json").is_file()
            and any((root / weights).rglob("*.safetensors"))
        ),
    )
    checks.append(
        Check(
            name="python",
            status="ok" if sys.version_info[:2] == (3, 12) else "error",
            detail=f"Python {platform.python_version()}; required 3.12.x",
        )
    )
    minimum_disk = thresholds.get("minimum_disk_gb", 100)
    checks.append(
        Check(
            name="disk",
            status="ok" if free >= minimum_disk else "error",
            detail=f"{free:.2f} GB free; required {minimum_disk} GB",
        )
    )
    minimum_ram = thresholds.get("minimum_local_memory_gb", 24)
    memory_ok = memory is not None and memory >= minimum_ram
    checks.append(
        Check(
            name="memory",
            status="ok" if memory_ok else "warning",
            detail=f"{memory} GiB; local target >= {minimum_ram} GiB",
        )
    )
    for name in ("uv", "mflux", "mlx"):
        checks.append(
            Check(
                name=name,
                status="ok" if versions[name] else "warning",
                detail=versions[name] or "Not installed; local setup incomplete",
            )
        )
    checks.append(
        Check(
            name="credentials",
            status="ok",
            detail=(
                "Only presence flags recorded; missing developer credentials are valid "
                "in keyless mode."
            ),
        )
    )
    checks.append(
        Check(
            name="codex",
            status="ok" if codex and auth_present else "warning",
            detail="CLI and auth file found; live access unverified"
            if codex and auth_present
            else "CLI or auth file missing; host tool may still be available",
        )
    )
    checks.append(
        Check(
            name="render",
            status="warning",
            detail=(
                f"Candidate: {render}; doctor does not generate an image or verify live access."
                if render
                else "No eligible renderer; host-agent plans only."
            ),
        )
    )
    checks.append(
        Check(
            name="api_budget",
            status="ok",
            detail=(
                f"Project API ceiling ${budget:.2f}; paid calls "
                f"{'enabled' if paid_enabled else 'disabled'}."
            ),
        )
    )
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date()
    for policy in sorted((root / "policies").rglob("*.md")):
        for line in policy.read_text().splitlines():
            if line.startswith("review_after:"):
                value = line.partition(":")[2].strip().strip("\"'")
                try:
                    expired = datetime.strptime(value, "%Y-%m-%d").date() < today
                    status = "warning" if expired else "ok"
                    detail = f"{policy.relative_to(root)}: review_after {value}"
                except ValueError:
                    status, detail = "warning", f"{policy.relative_to(root)}: invalid review_after"
                checks.append(Check(name="policy_review", status=status, detail=detail))
                break
    if (root / "policies").is_dir():
        from cardforge.policy.loader import lint

        policy_lint = lint(root)
        checks.append(
            Check(
                name="policy_lint",
                status="ok" if policy_lint["ok"] else "error",
                detail=(
                    f"{len(policy_lint['packs'])} packs valid"
                    if policy_lint["ok"]
                    else "; ".join(policy_lint["issues"][:3])
                ),
            )
        )
    if probe is None:
        probe = bool(thresholds.get("route_probe", False))
    routes: list[dict] = []
    if probe and credentials["OPENROUTER_API_KEY"]:
        from cardforge.openrouter_probe import probe as probe_routes

        routes = probe_routes(root)
        for r in routes:
            checks.append(Check(name=f"openrouter_route:{r['id']}", status=r["status"],
                                detail=f"{r['purpose']}: {r['detail']}"))  # fmt: skip
    checks.append(plugin_check(credentials["OPENROUTER_API_KEY"], routes, probed=bool(probe)))
    checks.append(ocr_check(root))
    if (root / "config/runtime.toml").exists():
        checks.append(runtime_check(root))
    from cardforge.autonomy import describe as autonomy_describe

    status, detail = autonomy_describe(root)
    checks.append(Check(name="autonomy", status=status, detail=detail))
    return DoctorReport(
        observed_at=datetime.now(ZoneInfo("Europe/Belgrade")).isoformat(),
        platform=platform.system(),
        architecture=platform.machine(),
        python=platform.python_version(),
        versions=versions,
        credentials=credentials,
        disk_free_gb=free,
        memory_gib=memory,
        codex_path=str(codex) if codex else None,
        codex_auth_file_present=auth_present,
        mode=mode,
        llm_provider=llm,
        render_profile=render,
        api_budget_usd=budget,
        paid_api_enabled=paid_enabled,
        checks=checks,
    )


def ocr_check(root: Path) -> Check:
    """Text checks need an OCR engine (stage 7): Apple Vision on macOS, RapidOCR elsewhere."""
    import importlib.util

    from cardforge.evaluate.ocr import ocr_engine

    try:
        engine = ocr_engine(root)
    except ValueError as exc:
        return Check(name="ocr", status="error", detail=str(exc))
    if engine == "rapidocr" and importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return Check(
            name="ocr",
            status="warning",
            detail="RapidOCR is not installed: text checks report `unknown`. "
            "Fix: uv sync --frozen --extra analysis",
        )
    return Check(name="ocr", status="ok", detail=f"engine {engine}")


def plugin_check(render_key: bool, routes: list[dict] | None = None, probed: bool = False) -> Check:
    """Stage 7 plugin mode (docs/stages/07-plugin.md): the render key decides full vs plans; a
    route the OpenRouter account refuses (guardrails, privacy, key, credits) blocks the full mode."""
    blocked = [r for r in routes or [] if r["status"] == "error"]
    if render_key and blocked:
        return Check(
            name="plugin_mode",
            status="error",
            detail="blocked: OpenRouter refuses " + "; ".join(f"{r['id']} ({r['setting']})" for r in blocked)
            + ". Fix the account settings named in the openrouter_route lines, then rerun doctor",
        )
    if render_key:
        return Check(
            name="plugin_mode",
            status="ok",
            detail="full: GPT Image 2.5 via OpenRouter; Nano Banana only as the reserve"
            + ("" if probed and routes else " (OpenRouter routes not probed)"),
        )
    return Check(
        name="plugin_mode",
        status="warning",
        detail=(
            "plan-only: no OPENROUTER_API_KEY. Fix: put the company key into the environment "
            "or into .env of the CardForge folder (OPENROUTER_API_KEY=...), then rerun doctor"
        ),
    )


def runtime_check(root: Path) -> Check:
    """Stage-4 core profiles parse; the default judge exists; no network is touched."""
    from cardforge.orchestrator.workspace import Workspace, judge_profile, render_profile
    from cardforge.prompting.assemble import load_template

    try:
        ws = Workspace.from_env(root)
        cfg = ws.config()
        renders = [render_profile(ws, name) for name in cfg["render_profiles"]]
        judge = judge_profile(ws, cfg["defaults"]["judge_profile"])
        template = load_template(root)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return Check(name="runtime_config", status="error", detail=str(exc))
    usable = [r.id for r in renders if r.available]
    return Check(
        name="runtime_config",
        status="ok",
        detail=(
            f"render profiles available: {', '.join(usable)}; default judge: {judge.id}; "
            f"prompt template {template.version}"
        ),
    )
