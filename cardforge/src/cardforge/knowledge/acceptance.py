"""Stage-1 acceptance evidence. No model, network or generation initialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cardforge.knowledge.catalog import LABEL_FIELDS, load_cards, read_jsonl, validate
from cardforge.knowledge.grammar import check_set


def stage1_acceptance(root: Path) -> dict:
    cards = load_cards(root)
    ids = {c.card_ref for c in cards}
    reviews = read_jsonl(root / "knowledge/catalog/human_review.jsonl")
    full = {r["card_ref"] for r in reviews if set(LABEL_FIELDS) <= set(r["confirmed_fields"])}
    policy = json.loads((root / "knowledge/catalog/acceptance_policy.json").read_text())
    disputes = read_jsonl(root / "knowledge/catalog/disagreements.jsonl")
    fresh = read_jsonl(root / "knowledge/catalog/disagreements.final-blind.jsonl")
    all_disputes = disputes + fresh
    current = {c.card_ref: c for c in cards}

    def resolution_matches(row: dict) -> bool:
        if row["card_ref"] not in current:
            return False
        value = current[row["card_ref"]].model_dump()
        for part in row["field"].split("."):
            value = value[part]
        resolved = row["resolution"]
        if isinstance(value, list):
            return isinstance(resolved, list) and sorted(value) == sorted(resolved)
        return value == resolved

    adjudicated = all(
        r["status"] == "resolved_by_human"
        and r["resolution"] is not None
        and r["card_ref"] in full
        and resolution_matches(r)
        for r in all_disputes
    )
    # The waiver is explicit and scoped. Test scores are never relabeled as passed.
    human_gate = (
        policy.get("scope") == "stage_1"
        and policy.get("criterion") == "full_human_adjudication"
        and policy.get("source") == "explicit_user_decision_in_chat"
        and policy.get("user_confirmation")
        == "Да, принять полное человеческое ревью вместо порога 90%"
        and full == ids
        and adjudicated
    )
    grammar = json.loads((root / "knowledge/set_grammar/v1.yaml").read_text())
    grammar_results = [
        check_set([c for c in cards if c.set_index == n], grammar) for n in range(1, 16)
    ]
    rule_examples = True
    for name in (
        "knowledge/set_grammar/v1.yaml",
        "knowledge/style_profile/reference_cards_v1.yaml",
    ):
        rules = json.loads((root / name).read_text())["rules"]
        rule_examples &= all(
            len(set(r["examples"])) >= 2 and set(r["examples"]) <= ids for r in rules
        )
    semantic = read_jsonl(root / "knowledge/frames/semantic_review.jsonl")
    safe = json.loads((root / "knowledge/frames/safe_zone.json").read_text())
    screenshot_hashes = {}
    for row in semantic:
        shot = row["screenshot"]
        if shot not in screenshot_hashes:
            screenshot_hashes[shot] = hashlib.sha256((root / shot).read_bytes()).hexdigest()
    semantic_valid = (
        len(semantic) == 160
        and {r["card_ref"] for r in semantic} == ids
        and all(
            r["source_sha256"] == current[r["card_ref"]].source_sha256
            and r["screenshot_sha256"] == screenshot_hashes[r["screenshot"]]
            and r["assessment"] in {"clear", "edge_support", "scene_detail"}
            and r["note"]
            and r["evidence"]
            and all((root / p).exists() for p in r["evidence"])
            for r in semantic
        )
        and bool(safe.get("semantic_audit"))
        and bool(safe.get("method"))
    )
    # Import already verifies pixels against source SHA; validate repeats source integrity.
    validation = validate(root, require_human=True)
    checks = {
        "catalog_and_human_category_theme": validation["valid"],
        "crosscheck_replaced_by_explicit_human_gate": bool(human_gate),
        "paid_api_calls_zero": policy.get("paid_api_calls") == 0,
        "all_15_reference_sets_explained": all(r["valid"] for r in grammar_results),
        "every_rule_has_two_examples": bool(rule_examples),
        "safe_zone_method_and_all_card_visual_audit": bool(semantic_valid),
        "alpha_conclusion_recorded": all(
            c.alpha_meaning in {"absent", "fully_opaque"} for c in cards
        ),
        "ambiguous_categories_human_adjudicated": bool(adjudicated),
    }
    return {
        "stage": 1,
        "complete": all(checks.values()),
        "checks": checks,
        "human_full_field_cards": len(full),
        "original_disagreements_resolved": len(disputes) if adjudicated else None,
        "fresh_disagreements_resolved": len(fresh) if adjudicated else None,
        "blind_threshold_claimed_met": False,
        "criterion_change": policy,
        "critical_identity_occlusions_observed": sum(
            r["critical_identity_occluded"] for r in semantic
        ),
        "noncritical_frame_contacts": sum(r["noticeable_noncritical_contact"] for r in semantic),
        "warning_count": len(validation["warnings"]),
    }
