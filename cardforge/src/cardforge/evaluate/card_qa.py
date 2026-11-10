"""Card QA order (TA §11.1): hard checks → PolicyInspector + detectors → judge → decision.

The judge is a pluggable profile (`config/runtime.toml [judges.*]`). The core does not pick
a working judge: with profile `none` a card that passes the checks is flagged for G2, and a
host-agent answer from a host that stage-2 calibration did not admit is flagged too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from cardforge.contracts.generation import CardEvaluation, JudgeOutcome
from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CardSpec, Hitl
from cardforge.evaluate.hard_checks import format_check
from cardforge.evaluate.look_features import look_signal
from cardforge.evaluate.mask_checks import CHECKS as MASK_CHECKS  # noqa: F401 (public name)
from cardforge.evaluate.mask_checks import mask_checks
from cardforge.evaluate.models import JudgeAnswer, Rubric
from cardforge.evaluate.object_mask import crop_check
from cardforge.policy import actions
from cardforge.policy.inspector import (
    inspector_task,
    qa_rules,
    run_detectors,
    verdicts_from_answers,
)

CRITERIA = list(Rubric.model_fields)
RUBRIC_VERSION = "rubric7-runtime-v1"
# Hard checks on the object mask: `crop` (phase 1, 2026-11-04) and safe zone, background
# simplicity, fill (`mask_checks.py`, 2026-11-05; limits in config/mask_checks.toml). A
# failure carries a reason code of prompts/render/fixes.toml, so the fix needs no judge.
CROP_CHECK = "crop"
LOOK_SIGNAL = "look"  # evaluate/look_features.py, signal only
CHECK_CODES = {"crop": "crop", "text": "text"}
FIX_QUESTION_PREFIX = "fix:"


@dataclass(frozen=True)
class JudgeProfile:
    """One `[judges.<id>]` entry: provider, model, profile (rubric/prompt version), enabled."""

    id: str
    provider: str
    model: str = "-"
    profile: str = "-"
    decision_policy: str = "definite-failure-first-v1"
    admitted: tuple[str, ...] = ()
    max_cost_usd: float | None = None  # per-judge ceiling inside the run budget
    test_only: bool = False
    enabled_in_config: bool = True
    fake: dict = field(default_factory=dict)
    # card: one task per card (N = 1); group / absolute: batch judges of candidate mode;
    # veto: one checklist task per card, shadow-only (logged, never decides).
    kind: str = "card"
    panel: bool = False  # joins runs as a logged judge without being named explicitly
    candidates: int = 1  # candidates per card position the profile is calibrated for
    route: str | None = None
    ship_floor: float | None = None
    estimate_per_item_usd: float = 0.0
    # veto: OpenRouter candidate of config/evaluation.toml and request limits (phase 2).
    candidate: str | None = None
    reasoning_effort: str | None = None
    max_input_tokens_ceiling: int | None = None
    max_output_tokens: int | None = None
    # J4 (user 2026-11-09): ask this judge only on cards in the style scorer's grey zone,
    # `min_p` ≤ p < τ_r of the card's profile; `runs` = "shadow" (shadow runs only) or "all".
    # Empty = every card. A card without a style score is still asked (no silent loss).
    style_gate: dict = field(default_factory=dict)
    note: str = ""

    @property
    def enabled(self) -> bool:
        return self.provider != "none"


@dataclass
class HardResult:
    checks: dict[str, dict] = field(default_factory=dict)

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.checks.items() if v["passed"] is False]

    @property
    def unknown(self) -> list[str]:
        return [k for k, v in self.checks.items() if v["passed"] is None and v.get("required")]

    @property
    def flagged(self) -> list[str]:
        """Flag-only checks that measured a failure (`config/mask_checks.toml` flag_only)."""
        return [
            k
            for k, v in self.checks.items()
            if v.get("flag_only") and v.get("would") == "fix" and not v.get("subsumed_by")
        ]


def flag_only_checks(mask_config: dict | None = None) -> frozenset[str]:
    """`flag_only` of config/mask_checks.toml (user 2026-11-09: crop, safe_zone)."""
    if mask_config is None:
        from cardforge.evaluate.mask_checks import load_config

        mask_config = load_config() or {}
    return frozenset(mask_config.get("flag_only", ()))


def demote_to_flags(result: HardResult, names: frozenset[str]) -> None:
    """A failed flag-only check stops being a hard fail: `passed` None, not required,
    `would: "fix"` and the measured verdict kept (`measured_passed`), so the person sees it on
    G2 and the shadow journal can still score it. A pass stays a pass."""
    for name in names:
        check = result.checks.get(name)
        if not check:
            continue
        failed = check.get("passed") is False or check.get("subsumed_by") is not None
        result.checks[name] = {
            **check,
            "flag_only": True,
            "would": "fix" if failed else None,
            "measured_passed": False if failed else check.get("passed"),
        }
        if failed:
            result.checks[name].update(passed=None, required=False)


def hard_checks(
    final: Path,
    spec: CardSpec,
    ocr: dict,
    mask=None,
    mask_source: str = "rembg-u2net",
    mask_config: dict | None = None,
) -> HardResult:
    """Format, text, the three mask checks and `crop`. `mask_config=None` reads
    `config/mask_checks.toml`; without it a present mask gives review, never a pass."""
    fmt = format_check(final)
    result = HardResult(
        {"format": {"passed": fmt["status"] == "pass", "value": fmt, "threshold": "430x480 PNG"}}
    )
    if ocr.get("status") == "ok":
        allowed = " ".join(spec.must_have).lower()
        text = [o["text"] for o in ocr.get("observations", []) if o["text"].lower() not in allowed]
        result.checks["text"] = {
            "passed": not text,
            "value": text[:10],
            "threshold": "0 characters outside must_have",
            "engine": ocr.get("engine", "unknown"),
        }
    else:
        # Unknown is review, never a pass (TA §11.2).
        result.checks["text"] = {
            "passed": None,
            "required": True,
            "value": ocr.get("reason") or "OCR unavailable",
            "threshold": "0 characters outside must_have",
        }
    for name, check in mask_checks(final, mask, spec.category, mask_config).items():
        result.checks[name] = {**check, "mask": mask_source}
    result.checks[CROP_CHECK] = crop_check(mask, spec.category, mask_source)
    zone = result.checks["safe_zone"]
    if zone["passed"] is False and result.checks[CROP_CHECK]["passed"] is False:
        # One defect, one reason: an object cut by the frame also enters the UI bands.
        # Not a pass: the measured failure stays visible and the fix comes from `crop`.
        result.checks["safe_zone"] = {
            **zone,
            "passed": None,
            "required": False,
            "subsumed_by": CROP_CHECK,
            "note": "failed together with crop (same code); reported once as crop",
        }
    for name, code in CHECK_CODES.items():
        if name in result.checks:
            result.checks[name].setdefault("code", code)
    demote_to_flags(result, flag_only_checks(mask_config))
    # Signal only (2026-11-06, docs/research/generation-defects.md §7.6): haze, pure colour,
    # small highlights; passed=None and required=False, so it never changes the decision.
    result.checks[LOOK_SIGNAL] = {**look_signal(final, mask), "mask": mask_source}
    return result


def inspect_task(
    spec: CardSpec,
    image_rel: str,
    rules,
    fix_questions: dict[str, str] | None = None,
    attempt_no: int | None = None,
) -> dict:
    """Policy questions plus, after a fix, one targeted question per fix code
    (`fix:<code>`; "yes" = the defect is still there). Without fix questions the task is
    exactly the old one, so answers saved for earlier runs stay valid."""
    task = inspector_task(spec.card_id, image_rel, rules)
    if fix_questions:
        task["questions"] = task["questions"] + [
            {"rule_id": FIX_QUESTION_PREFIX + code, "question": q}
            for code, q in fix_questions.items()
        ]
        task["attempt_no"] = attempt_no
    return {"step": "policy_inspect", **task}


def fix_checks(answers: dict, asked: dict[str, int]) -> dict[str, dict]:
    """Answers to the `fix:<code>` questions; a missing answer is uncertain, never fixed."""
    out = {}
    for code, since in asked.items():
        answer = answers.get(FIX_QUESTION_PREFIX + code, {})
        word = str(answer.get("answer", "uncertain")).lower()
        out[code] = {
            "answer": word if word in ("yes", "no") else "uncertain",
            "evidence": str(answer.get("evidence") or "no inspector answer"),
            "resolved": {"no": True, "yes": False}.get(word),
            "fix_requested_for_attempt": since,
        }
    return out


def policy_verdicts(
    spec: CardSpec, rules, answers: dict, ocr: dict, packs: list[PolicyPack], hitl: Hitl
) -> list[PolicyVerdict]:
    return verdicts_from_answers(spec.card_id, rules, answers, hitl) + run_detectors(
        spec.card_id, ocr, packs, hitl
    )


def applicable_inspector_rules(packs, plan_verdicts: list[PolicyVerdict], spec: CardSpec):
    return qa_rules(packs, plan_verdicts, {spec.card_id, "request"})


def judge_task(
    spec: CardSpec,
    *,
    set_theme: str,
    attempt_no: int,
    image_rel: str,
    thumb_rel: str,
    image_sha256: str,
    profile: JudgeProfile,
) -> dict:
    return {
        "step": "judge",
        "schema_version": RUBRIC_VERSION,
        "instruction": (
            "Judge the card image against its spec. For every criterion answer passed "
            "true/false with a short note; style_uncertain=true when you cannot tell whether "
            "the style matches the references. Any failed criterion needs a concrete, positive "
            "fix_instruction. Use the thumbnail for readability."
        ),
        "card_id": spec.card_id,
        "attempt_no": attempt_no,
        "set_theme": set_theme,
        "image": image_rel,
        "thumbnail": thumb_rel,
        "image_sha256": image_sha256,
        "spec": spec.model_dump(mode="json", exclude={"policy_flags", "seed"}),
        "criteria": CRITERIA,
        "judge_profile": profile.profile,
        "decision_policy": profile.decision_policy,
        "answer_schema": JudgeAnswer.model_json_schema(),
    }


def judge_outcome(data: dict, provider: str, model: str, profile: JudgeProfile) -> JudgeOutcome:
    try:
        answer = JudgeAnswer.model_validate(data)
    except ValidationError as exc:
        return JudgeOutcome(
            judge_model=f"{provider}:{model}",
            decision="flag",
            decision_policy=profile.decision_policy,
            issues=[f"invalid judge answer: {exc.errors()[0]['msg']}"],
        )
    decision = answer.decision_for(profile.decision_policy)
    judge_id = f"{provider}:{model}"
    issues = list(answer.issues)
    if profile.admitted and judge_id not in profile.admitted:
        issues.append(f"{judge_id} is not an admitted judge for profile {profile.id}")
        decision = "flag"
    return JudgeOutcome(
        criteria={k: v.model_dump() for k, v in answer.criteria.__dict__.items()},
        issues=issues,
        fix_instruction=answer.fix_instruction,
        judge_model=judge_id,
        decision=decision,
        decision_policy=profile.decision_policy,
    )


def decide(
    hard: HardResult,
    verdicts: list[PolicyVerdict],
    judge: JudgeOutcome | None,
    profile: JudgeProfile,
    rule_constraints: dict[str, str | None],
    checked_fixes: dict[str, dict] | None = None,
    auto_rejects: list[dict] | None = None,
) -> CardEvaluation:
    """`auto_rejects`: acting step-0 vetoes / style-score zone (evaluate/auto_reject.py), each
    {source, code}; any of them makes the card `fix` with its reason code (no person)."""
    reasons: list[str] = []
    fix: list[str] = []
    fixes: list[dict] = []
    checked_fixes = checked_fixes or {}
    decision = "accept"
    if hard.failed:
        decision = "fix"
        reasons += [f"hard_check:{name}" for name in hard.failed]
        for name in hard.failed:
            code = hard.checks[name].get("code")
            entry = {"source": f"hard_check:{name}", "codes": [code] if code else []}
            if hard.checks[name].get("fix_text"):
                entry["text"] = hard.checks[name]["fix_text"]
            fixes.append(entry)
    policy_fix = [v for v in verdicts if v.action in (actions.FIX, actions.REGENERATE)]
    policy_flag = [v for v in verdicts if v.action == actions.FLAG_G2]
    if policy_fix:
        decision = "fix"
        reasons += [f"policy:{v.rule_id}:{v.action}" for v in policy_fix]
        fix += [rule_constraints.get(v.rule_id) or "" for v in policy_fix]
        fixes += [
            {"source": f"policy:{v.rule_id}", "codes": [], "text": rule_constraints.get(v.rule_id)}
            for v in policy_fix
        ]
    # A fix asked for an earlier attempt that the inspector still sees is a failed check.
    unresolved = [c for c, r in checked_fixes.items() if r["resolved"] is False]
    unsure = [c for c, r in checked_fixes.items() if r["resolved"] is None]
    if unresolved:
        decision = "fix"
        reasons += [f"fix_check:{c}:unresolved" for c in unresolved]
        fixes.append({"source": "fix_check", "codes": unresolved})
    if auto_rejects:
        decision = "fix"
        reasons += [f"auto_reject:{f['source']}" for f in auto_rejects]
        codes = list(dict.fromkeys(f["code"] for f in auto_rejects if f.get("code")))
        fixes.append({"source": "auto_reject", "codes": codes})
    if decision == "accept":
        if judge is None:
            decision = "flag"
            reasons.append(
                "judge:not_run (no judge profile)" if not profile.enabled else "judge:not_run"
            )
        elif judge.decision != "accept":
            decision = judge.decision
            reasons.append(f"judge:{judge.decision}")
            if judge.fix_instruction:
                fix.append(judge.fix_instruction)
                fixes.append({"source": "judge", "codes": [], "text": judge.fix_instruction})
        if policy_flag:
            reasons += [f"policy:{v.rule_id}:flag_g2" for v in policy_flag]
            if decision == "accept":
                decision = "flag"
        if hard.unknown:
            reasons += [f"hard_check:{name}:unknown" for name in hard.unknown]
            if decision == "accept":
                decision = "flag"
        if hard.flagged:  # crop / safe_zone: for the person, no redraw (user 2026-11-09)
            reasons += [f"hard_check:{name}:flag" for name in hard.flagged]
            if decision == "accept":
                decision = "flag"
        if unsure:  # unknown is review, never a pass (TA §11.2)
            reasons += [f"fix_check:{c}:uncertain" for c in unsure]
            if decision == "accept":
                decision = "flag"
    notes = [f"policy:{v.rule_id}:note" for v in verdicts if v.action == actions.ACCEPT_NOTE]
    return CardEvaluation(
        hard_checks=hard.checks,
        policy_verdicts=verdicts,
        judge=judge,
        decision=decision,
        reasons=reasons + notes,
        fix_instruction=" ".join(x for x in fix if x) or None,
        fixes=fixes,
        fix_checks=checked_fixes,
    )
