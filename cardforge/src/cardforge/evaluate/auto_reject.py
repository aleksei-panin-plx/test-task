"""Step 0 of auto-reject (docs/research/fable-review.md §3.4): precise vetoes act in the core.

Admitted vetoes (config/auto_reject.toml, re-checked on the blind labels by
`scripts/stage04_style_scorer_veto.py recheck`: precision ≥ 90 % on ≥ 20 firings, as a rule):

- veto-judge items `style_mismatch` ∨ `downward` of the checklist profile they were measured
  with (`defect-checklist-v2`, Kimi K3.1 Pro, blind-labels-02: 43/44). An item of another
  profile (e.g. the runtime v1 `kimi-veto`) is a different judge: journaled as a shadow firing
  (`acted = false`), never acts. `second_ground_line` (16/19 = 84 %) is a shadow rule.
- hard check `text` was proposed but measured 19/26 = 73 %: not a step-0 veto (it stays an
  ordinary hard check of the core). `[hard_checks]` of the config can admit a hard check later.

An acting firing turns the card decision into `fix` with the item's reason code
(`prompts/render/fixes.toml`), so the card is regenerated instead of reaching a person.
Every firing (acting or not) is journaled as a `check_result` event with
`check = "auto_reject"`; `monitor` of the script measures precision on the next 100 labelled
firings (continue iff ≥ 85 %) and can stop the rule through the state file
(`stopped = true` → firings are logged, never acted on). Switching the rule off:
`enabled = false` in the config, or the run kill switch `runs/STOP` as for any run.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

CONFIG = "config/auto_reject.toml"
CHECK = "auto_reject"
EVENT = "check_result"
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


def load_config(root: Path | None = None) -> dict | None:
    path = (root or _PACKAGE_ROOT) / CONFIG
    return tomllib.loads(path.read_text()) if path.is_file() else None


def stopped(root: Path, cfg: dict) -> bool:
    """The monitor's state file says the continuation rule failed."""
    path = root / cfg.get("state", "")
    if not cfg.get("state") or not path.is_file():
        return False
    try:
        return bool(json.loads(path.read_text()).get("stopped"))
    except (OSError, ValueError):
        return True  # an unreadable state is not permission to act


def active(root: Path, cfg: dict | None) -> bool:
    return bool(cfg and cfg.get("enabled", False) and not stopped(root, cfg))


def _rules(cfg: dict, key: str = "veto_rules") -> dict[str, dict[str, str]]:
    """checklist profile → {item: reason code} of `veto_rules` (admitted) or `shadow_rules`."""
    out: dict[str, dict[str, str]] = {}
    for r in cfg.get(key, []):
        out.setdefault(r["judge_profile"], {}).update(r["items"])
    return out


def veto_firings(shadows: dict, profiles: dict[str, str], cfg: dict | None) -> list[dict]:
    """Watched items (admitted and shadow rules) that fired in the veto judges' outcomes.

    `shadows`: judge id → JudgeOutcome (or a status string: not answered, ignored);
    `profiles`: judge id → checklist profile of that judge. Each row:
    {source, judge, profile, item, code, note, admitted}; `admitted` = the (profile, item)
    pair is in `veto_rules` (measured precision); every other firing is shadow-only."""
    if not cfg:
        return []
    admitted_rules = _rules(cfg)
    shadow_rules = _rules(cfg, "shadow_rules")
    codes: dict[str, str] = {}
    for rules in (shadow_rules, admitted_rules):
        for items in rules.values():
            codes.update(items)
    out = []
    for judge, outcome in shadows.items():
        criteria = getattr(outcome, "criteria", None)
        if not criteria:
            continue
        profile = profiles.get(judge, "?")
        for item in sorted(codes):
            answer = criteria.get(item)
            if not isinstance(answer, dict) or not answer.get("defect"):
                continue
            out.append(
                {
                    "source": f"{judge}:{item}",
                    "judge": judge,
                    "profile": profile,
                    "item": item,
                    "code": codes[item],
                    "note": str(answer.get("note", ""))[:300],
                    "admitted": item in admitted_rules.get(profile, {}),
                }
            )
    return out


def hard_firings(hard_checks: dict, cfg: dict | None) -> list[dict]:
    """Admitted hard checks that failed (they already decide `fix` in the core)."""
    if not cfg:
        return []
    out = []
    for name, code in cfg.get("hard_checks", {}).items():
        check = hard_checks.get(name)
        if check and check.get("passed") is False:
            out.append({"source": f"hard_check:{name}", "item": name, "code": code,
                        "admitted": True, "note": str(check.get("value"))[:300]})  # fmt: skip
    return out


def acting(firings: list[dict], is_active: bool) -> list[dict]:
    """The firings that change the decision: admitted ones, while the rule is active."""
    return [f for f in firings if f["admitted"] and is_active] if is_active else []


def event_payload(
    firings: list[dict], acted: list[dict], *, attempt_no: int, image: str, image_sha256: str,
    cfg: dict, is_active: bool,
) -> dict:  # fmt: skip
    return {
        "check": CHECK,
        "passed": not acted,
        "attempt_no": attempt_no,
        "acted": bool(acted),
        "active": is_active,
        "config": cfg.get("version", "?"),
        "vetoes": [f["source"] for f in acted],
        "shadow": [f["source"] for f in firings if f not in acted],
        "codes": list(dict.fromkeys(f["code"] for f in acted if f.get("code"))),
        "image": image,
        "image_sha256": image_sha256,
    }
