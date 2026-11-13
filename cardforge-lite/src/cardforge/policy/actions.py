"""Severity × stage × gate mode → action (TA §9.5)."""

from __future__ import annotations

from cardforge.contracts.request import Hitl

# Plan stage
REPLACE = "replace"  # planner swaps the object for an alternative and screens again
FLAG_G1 = "flag_g1"
# QA stage
FIX = "fix_or_regenerate"  # hard failure; after max_fix_rounds the card is rejected
FLAG_G2 = "flag_g2"
REGENERATE = "regenerate_with_fix"
ACCEPT_NOTE = "accept_with_note"
# Any stage
NOTE = "note"
PASS = "none"


def action_for(severity: str, verdict: str, stage: str, hitl: Hitl) -> str:
    if verdict == "pass":
        return PASS
    # An inspector's `uncertain` is treated as `review`; it never relaxes a block.
    if verdict == "uncertain" and severity == "advise":
        severity = "review"
    if stage == "plan":
        # Light (user 2026-11-13): every plan restriction counts — block, review and advise alike.
        # An object that matches any plan rule is replaced and the replacement goes to policy_notes.
        return REPLACE
    if severity == "advise":
        return NOTE
    if stage == "prompt":
        return REPLACE
    if severity == "block":
        return FIX
    if hitl.g2 != "none":
        return FLAG_G2
    return REGENERATE if hitl.review_policy == "replace" else ACCEPT_NOTE


def blocks_plan(action: str) -> bool:
    """A plan with any `replace` action is not ready for G1 or rendering."""
    return action == REPLACE
