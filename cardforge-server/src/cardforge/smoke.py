"""Report prerequisites honestly; an unexecuted check is never a passing smoke test."""

import tomllib
from pathlib import Path

from cardforge.doctor import collect


def preflight(root: Path) -> dict:
    report = collect(root)
    config = tomllib.loads((root / "config/models.toml").read_text())
    project = root / "config/cardforge.toml"
    development = (
        tomllib.loads(project.read_text()).get("development", {}) if project.exists() else {}
    )
    checks = []
    for name in ("gemini", "closed-nano"):
        reasons = []
        if not report.paid_api_enabled:
            reasons.append("paid_api_disabled")
        if not report.credentials["GEMINI_API_KEY"]:
            reasons.append("missing_gemini_api_key")
        if not report.versions["google-genai"]:
            reasons.append("missing_google_genai")
        if name == "closed-nano":
            if development.get("local_generation_only"):
                reasons.append("local_generation_only")
            if config["models"][name].get("enabled") is False:
                reasons.append("model_profile_disabled")
        checks.append(
            {
                "profile": name,
                "status": "blocked" if reasons else "ready",
                "reasons": reasons,
                "executed": False,
            }
        )
    for name, model in config["models"].items():
        if model["backend"] != "mflux":
            continue
        reasons = []
        if report.platform != "Darwin" or report.architecture != "arm64":
            reasons.append("requires_apple_silicon")
        if not report.versions["mflux"] or not report.versions["mlx"]:
            reasons.append("missing_local_runtime")
        if not model.get("license_accepted", False):
            reasons.append("license_not_confirmed_by_user")
        if model.get("revision") == "unresolved":
            reasons.append("weights_revision_not_pinned")
        checks.append(
            {
                "profile": name,
                "status": "blocked" if reasons else "ready",
                "reasons": reasons,
                "executed": False,
            }
        )
    return {
        "schema_version": 1,
        "api_spent_usd": 0,
        "api_spend_scope": "This preflight invocation only; no provider calls.",
        "checks": checks,
    }
