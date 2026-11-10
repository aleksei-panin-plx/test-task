"""Calibration metrics with explicit missing data and fail-closed runtime gating."""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from pathlib import Path

from cardforge.evaluate.judge_pack import load_manifest
from cardforge.evaluate.models import JudgeAnswer, JudgeIdentity, Label
from cardforge.knowledge.catalog import load_cards, read_jsonl


def cohen_kappa(truth: list[bool], predicted: list[bool]) -> float | None:
    if len(truth) != len(predicted) or not truth:
        raise ValueError("nonempty paired decisions required")
    n = len(truth)
    observed = sum(a == b for a, b in zip(truth, predicted, strict=True)) / n
    a, b = sum(truth) / n, sum(predicted) / n
    expected = a * b + (1 - a) * (1 - b)
    return (observed - expected) / (1 - expected) if expected < 1 else None


def roc_auc(positive: list[float], negative: list[float]) -> float | None:
    """Higher score = more reference-like; ties contribute half a concordant pair."""
    import math

    if not positive or not negative:
        return None
    if not all(math.isfinite(s) for s in positive + negative):
        raise ValueError("nonfinite style score")
    return sum((p > n) + 0.5 * (p == n) for p in positive for n in negative) / (
        len(positive) * len(negative)
    )


def calibrate(
    root: Path,
    judge_id: str,
    *,
    role: str = "working",
    pack_id: str | None = None,
    pack_ids: list[str] | None = None,
    decision_policy: str = "flag-first",
) -> dict:
    """Score one identity on explicit disjoint packs from one consistent campaign."""
    if role not in ("working", "independent"):
        raise ValueError("unknown judge role")
    if decision_policy not in ("flag-first", "definite-failure-first-v1"):
        raise ValueError("unknown judge decision policy")
    selected_packs = _selected_packs(pack_id=pack_id, pack_ids=pack_ids)
    items = {r["item_id"]: r for r in load_manifest(root, root / "calibration/manifest.json")}
    labels = [Label.model_validate(r) for r in read_jsonl(root / "calibration/labels.jsonl")]
    if len({r.item_id for r in labels}) != len(labels):
        raise ValueError("duplicate calibration ground truth")
    records = [
        r
        for path in sorted((root / "calibration/judgements").glob("*.jsonl"))
        for r in read_jsonl(path)
        if ":".join(r["judge"][k] for k in ("provider", "host", "model")) == judge_id
        and r["judge"]["role"] == role
        and (selected_packs is None or r.get("pack_id") in selected_packs)
    ]
    by_id = {r["item_id"]: r for r in records}
    if len(by_id) != len(records):
        raise ValueError("overlapping judge packs; select disjoint packs with pack_id or pack_ids")
    identities = {json.dumps(r["judge"], sort_keys=True) for r in records}
    if len(identities) > 1:
        raise ValueError("one judge ID must identify exactly one role/family")
    rubric_versions = {r.get("rubric_version", "rubric7-v1") for r in records}
    if len(rubric_versions) > 1:
        raise ValueError("different rubric versions require separate calibration campaigns")
    input_versions = {r.get("input_contract_version", "judge-inputs-v1") for r in records}
    if len(input_versions) > 1:
        raise ValueError("different judge inputs require separate calibration campaigns")
    inference_settings = {
        json.dumps(r.get("judge_run_settings", {}), sort_keys=True) for r in records
    }
    if len(inference_settings) > 1:
        raise ValueError("different inference settings require separate calibration campaigns")
    decisions = {}
    for row in records:
        JudgeIdentity.model_validate(row["judge"])
        answer = JudgeAnswer.model_validate(row["answer"])
        if answer.decision != row["decision"]:
            raise ValueError("stored decision contradicts judge criteria")
        decisions[row["item_id"]] = answer.decision_for(decision_policy)
    accepted_real, rejected_foreign, borderline = [], [], []
    missing = []
    for label in labels:
        item = items.get(label.item_id)
        if not item or (item["image_sha256"], item["cohort"]) != (label.image_sha256, label.cohort):
            raise ValueError("ground truth does not match current manifest")
        row = by_id.get(label.item_id)
        if row is None:
            missing.append(label.item_id)
            continue
        if row["image_sha256"] != label.image_sha256 or row["cohort"] != label.cohort:
            raise ValueError("judge/label provenance mismatch")
        decision = decisions[label.item_id]
        accepted = decision == "accept"
        if label.cohort == "real":
            accepted_real.append(accepted)
        elif label.cohort == "foreign":
            rejected_foreign.append(decision in ("fix", "reject"))
        elif label.source == "human":
            # An unresolved flag does not become an observed binary decision.
            if decision != "flag":
                borderline.append((label.accepted, accepted))
    real_rate = sum(accepted_real) / len(accepted_real) if accepted_real else None
    foreign_rate = sum(rejected_foreign) / len(rejected_foreign) if rejected_foreign else None
    kappa = (
        cohen_kappa([a for a, _ in borderline], [b for _, b in borderline]) if borderline else None
    )
    missing_labels = sorted(set(items) - {r.item_id for r in labels})
    required_refs = {c.card_ref for c in load_cards(root)}
    reference_coverage = required_refs.issubset(
        {r.item_id for r in labels if r.cohort == "real"} & set(by_id)
    )
    passed = (
        role == "working"
        and not missing
        and not missing_labels
        and reference_coverage
        and len(accepted_real) >= 160
        and len(rejected_foreign) >= 30
        and len(borderline) >= 40
        and real_rate >= 0.9
        and foreign_rate >= 0.9
        and kappa is not None
        and kappa >= 0.6
    )
    return {
        "judge_id": judge_id,
        "role": role,
        "pack_id": pack_id,
        "pack_ids": sorted({r.get("pack_id") for r in records}, key=lambda value: value or ""),
        "judgement_records_n": len(records),
        "rubric_version": next(iter(rubric_versions), None),
        "input_contract_version": next(iter(input_versions), None),
        "judge_run_settings": json.loads(next(iter(inference_settings)))
        if inference_settings
        else None,
        "decision_policy": decision_policy,
        "raw_flag_n": sum(row["decision"] == "flag" for row in records),
        "projected_flag_n": sum(value == "flag" for value in decisions.values()),
        "real_n": len(accepted_real),
        "real_accept_rate": real_rate,
        "foreign_n": len(rejected_foreign),
        "foreign_reject_rate": foreign_rate,
        "borderline_binary_n": len(borderline),
        "cohen_kappa": kappa,
        "missing_ids": missing,
        "missing_labels": missing_labels,
        "all_reference_cards_judged": reference_coverage,
        "calibrated": passed,
        "runtime_allowed": passed,
        "note": "Projected flags count against real acceptance/foreign "
        "rejection and do not count toward 40 binary human-paired borderline observations.",
    }


def _validate_pack_id(pack_id: str) -> None:
    if (
        not isinstance(pack_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", pack_id) is None
    ):
        raise ValueError("invalid calibration pack_id")


def _selected_packs(
    *, pack_id: str | None = None, pack_ids: list[str] | None = None
) -> frozenset[str] | None:
    if pack_id is not None and pack_ids is not None:
        raise ValueError("select only one of pack_id and pack_ids")
    if pack_id is not None:
        _validate_pack_id(pack_id)
        return frozenset([pack_id])
    if pack_ids is None:
        return None
    if not isinstance(pack_ids, list) or not pack_ids:
        raise ValueError("pack_ids must be a nonempty list of unique valid IDs")
    for identifier in pack_ids:
        _validate_pack_id(identifier)
    if len(set(pack_ids)) != len(pack_ids):
        raise ValueError("pack_ids must contain unique IDs")
    return frozenset(pack_ids)


def select_working_judges(root: Path, *, config_path: Path | None = None) -> dict:
    """Read explicit profiles; discovery is retained only when the section is absent.

    Each [[working_judges]] table contains judge_id and optionally pack_id or pack_ids.
    Omitting both keeps the legacy nonoverlapping-pack aggregation for that identity.
    Candidate diagnostics describe every imported working campaign without combining its
    answers with another pack or selecting a profile based on calibration results.
    """
    config = config_path if config_path is not None else Path("config/evaluation.toml")
    config = config if config.is_absolute() else root / config
    settings = (
        tomllib.loads(config.read_text()) if config_path is not None or config.exists() else {}
    )
    imported = [
        (str(path.relative_to(root)), row)
        for path in sorted((root / "calibration/judgements").glob("*.jsonl"))
        for row in read_jsonl(path)
        if row["judge"]["role"] == "working"
    ]
    discovered = {
        ":".join(row["judge"][key] for key in ("provider", "host", "model")) for _, row in imported
    }
    explicit = "working_judges" in settings
    if explicit:
        profiles = settings["working_judges"]
        if not isinstance(profiles, list) or not profiles:
            raise ValueError("working_judges must be a nonempty array of tables")
        seen = set()
        for profile in profiles:
            if not isinstance(profile, dict) or set(profile) - {
                "judge_id",
                "pack_id",
                "pack_ids",
                "decision_policy",
            }:
                raise ValueError(
                    "working_judges entries allow only judge_id, pack_id, "
                    "pack_ids and decision_policy"
                )
            if profile.get("decision_policy", "flag-first") not in (
                "flag-first",
                "definite-failure-first-v1",
            ):
                raise ValueError("unknown judge decision policy")
            identifier = profile.get("judge_id")
            parts = identifier.split(":") if isinstance(identifier, str) else []
            if len(parts) != 3 or parts[0] not in ("host-agent", "gemini") or not all(parts):
                raise ValueError("working_judges judge_id must be provider:host:model")
            selected_packs = _selected_packs(
                pack_id=profile.get("pack_id"), pack_ids=profile.get("pack_ids")
            )
            selection = (
                identifier,
                tuple(sorted(selected_packs)) if selected_packs is not None else None,
            )
            if selection in seen:
                raise ValueError("duplicate working_judges selection")
            seen.add(selection)
    else:
        profiles = [{"judge_id": identifier} for identifier in sorted(discovered)]
    groups = defaultdict(list)
    for path, row in imported:
        identifier = ":".join(row["judge"][key] for key in ("provider", "host", "model"))
        groups[identifier, row.get("pack_id")].append((path, row))
    candidates = []
    for (identifier, pack_id), rows in sorted(
        groups.items(), key=lambda entry: (entry[0][0], entry[0][1] or "")
    ):
        item_ids = [row["item_id"] for _, row in rows]
        candidates.append(
            {
                "judge_id": identifier,
                "pack_id": pack_id,
                "selected": any(
                    profile["judge_id"] == identifier
                    and (
                        ("pack_id" not in profile and "pack_ids" not in profile)
                        or ("pack_id" in profile and profile["pack_id"] == pack_id)
                        or pack_id in profile.get("pack_ids", [])
                    )
                    for profile in profiles
                ),
                "records_n": len(rows),
                "unique_items_n": len(set(item_ids)),
                "source_files": sorted({path for path, _ in rows}),
                "rubric_versions": sorted(
                    {row.get("rubric_version", "rubric7-v1") for _, row in rows}
                ),
                "input_contract_versions": sorted(
                    {row.get("input_contract_version", "judge-inputs-v1") for _, row in rows}
                ),
                "judge_run_settings": [
                    json.loads(value)
                    for value in sorted(
                        {
                            json.dumps(row.get("judge_run_settings", {}), sort_keys=True)
                            for _, row in rows
                        }
                    )
                ],
            }
        )
    return {
        "selection_mode": "explicit" if explicit else "legacy-all-working",
        "config_path": str(config) if config.exists() else None,
        "profiles": profiles,
        "candidate_campaigns": candidates,
        "note": "Only selected profiles contribute to working-judge gates. Candidates are listed "
        "for provenance; no profile is selected or ranked by its observed metrics.",
    }


def require_calibrated(report: dict, judge_id: str) -> None:
    if report.get("judge_id") != judge_id or not report.get("runtime_allowed"):
        raise ValueError(f"judge is not calibrated for runtime: {judge_id}")


def project_runtime_answer(answer: JudgeAnswer, report: dict, judge_id: str) -> dict:
    """Apply the exact calibrated decision policy; retain the independent style warning."""
    require_calibrated(report, judge_id)
    policy = report.get("decision_policy", "flag-first")
    return {
        **answer.model_dump(),
        "decision": answer.decision_for(policy),
        "decision_policy": policy,
    }
