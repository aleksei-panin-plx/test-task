"""Autonomy hooks of TA §12.3: `config/autonomy.toml`, the kill switch and external actions.

Nothing here turns autonomy on. External actions (Miro, GitHub, game engine) are off by default:
an action is allowed only when the config enables it AND the person confirmed it in this call.
A missing or partial config falls back to the safe defaults below.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

CONFIG = "config/autonomy.toml"
EXTERNAL_ACTIONS = ("miro", "github", "engine")

DEFAULTS: dict = {
    "version": "autonomy-defaults",
    "schedule": {"trigger": "manual"},
    "budget": {"max_run_usd": 4.5, "max_campaign_usd": 12.0},
    "stop": {
        "kill_switch_file": "runs/STOP",
        "kill_switch_env": "CARDFORGE_STOP",
        "g3_return_max": 0.20,
        "g3_window": 100,
    },
    "audit": {"sample_share": 0.10},
    "external_actions": {"require_confirmation": True, **dict.fromkeys(EXTERNAL_ACTIONS, False)},
    "readiness": {"criteria": {}, "scan": {}, "campaigns": []},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_autonomy(root: Path) -> dict:
    """The autonomy config merged over the safe defaults (no file → defaults)."""
    path = root / CONFIG
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = _merge(DEFAULTS, data)
    ext = cfg["external_actions"]
    unknown = sorted(set(ext) - {"require_confirmation", *EXTERNAL_ACTIONS})
    if unknown:
        raise ValueError(f"{CONFIG}: unknown external actions {unknown}")
    for name in EXTERNAL_ACTIONS:
        if not isinstance(ext[name], bool):
            raise ValueError(f"{CONFIG}: external_actions.{name} must be true/false")
    # Confirmation by the person cannot be switched off from the file.
    ext["require_confirmation"] = True
    return cfg


def external_action_allowed(root: Path, action: str, *, confirmed: bool = False) -> bool:
    """True only for a known action that the config enables and the person confirmed."""
    if action not in EXTERNAL_ACTIONS:
        return False
    ext = load_autonomy(root)["external_actions"]
    return bool(ext[action]) and confirmed


def kill_switch_active(root: Path) -> bool:
    """`runs/STOP` (the path from the config) or `CARDFORGE_STOP=1`."""
    stop = load_autonomy(root)["stop"]
    if os.environ.get(stop["kill_switch_env"]) == "1":
        return True
    return (root / stop["kill_switch_file"]).exists()


def describe(root: Path) -> tuple[str, str]:
    """(status, detail) for `cardforge doctor`."""
    try:
        cfg = load_autonomy(root)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return "error", str(exc)
    on = [a for a in EXTERNAL_ACTIONS if cfg["external_actions"][a]]
    source = CONFIG if (root / CONFIG).exists() else "defaults (no config/autonomy.toml)"
    stop = "ACTIVE" if kill_switch_active(root) else "off"
    detail = (
        f"{source}; trigger {cfg['schedule']['trigger']}; audit "
        f"{cfg['audit']['sample_share']:.0%}; external actions "
        f"{', '.join(on) + ' (need confirmation)' if on else 'all off'}; kill switch {stop}"
    )
    return ("warning" if on or stop == "ACTIVE" else "ok"), detail


G1_MODES = {"human": "all", "auto": "none", "all": "all", "flagged": "flagged", "none": "none"}
G2_MODES = ("none", "flagged", "all")


def gate_modes(root: Path, g1: str | None = None, g2: str | None = None) -> tuple[str, str]:
    """G1/G2 modes of a new run: `config/autonomy.toml [gates]` (user 2026-11-10: G1 = human,
    G2 = none, G3 = human), overridden per run. G1 `human` → `all`, `auto` → `none`."""
    import tomllib

    path = Path(root) / "config/autonomy.toml"
    cfg = tomllib.loads(path.read_text(encoding="utf-8")).get("gates", {}) if path.exists() else {}
    g1 = g1 or cfg.get("g1", "human")
    g2 = g2 or cfg.get("g2", "none")
    if g1 not in G1_MODES:
        raise ValueError(f"g1 must be human | auto, got {g1!r}")
    if g2 not in G2_MODES:
        raise ValueError(f"g2 must be one of {G2_MODES}, got {g2!r}")
    return G1_MODES[g1], g2
