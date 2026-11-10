"""PolicyInspector questions and deterministic OCR detectors (TA §9.4)."""

from __future__ import annotations

import re

from cardforge.contracts.policy import PolicyPack, PolicyRule, PolicyVerdict
from cardforge.contracts.request import Hitl
from cardforge.policy.actions import action_for

_SCRIPTS = {
    "han": re.compile(r"[㐀-䶿一-鿿豈-﫿]"),
    "latin": re.compile(r"[A-Za-z]"),
}


def qa_rules(
    packs: list[PolicyPack], verdicts: list[PolicyVerdict], targets: set[str]
) -> list[PolicyRule]:
    """Only applicable questions: IP and text rules always, others when they fired upstream."""
    fired = {v.rule_id for v in verdicts if v.verdict != "pass" and v.target in targets}
    out = []
    for pack in packs:
        for rule in pack.rules:
            if "qa" not in rule.stages or not rule.qa_question:
                continue
            if "character" in rule.applies_to and set(rule.applies_to) == {"character"}:
                continue  # object cards (categories 1-4) have no characters
            if pack.policy_kind == "ip" or "text" in rule.applies_to or rule.id in fired:
                out.append(rule)
    return out


def inspector_task(card_id: str, image_path: str, rules: list[PolicyRule]) -> dict:
    return {
        "schema_version": "policy_inspect.v1",
        "card_id": card_id,
        "image": image_path,
        "instruction": "Answer each question about the image with yes, no or uncertain "
        "and one sentence of visual evidence.",
        "questions": [{"rule_id": r.id, "question": r.qa_question} for r in rules],
    }


def verdicts_from_answers(
    card_id: str, rules: list[PolicyRule], answers: dict[str, dict], hitl: Hitl
) -> list[PolicyVerdict]:
    """Map yes/no/uncertain to verdicts; a missing answer is `uncertain`, never a pass."""
    out = []
    for rule in rules:
        answer = answers.get(rule.id, {})
        word = str(answer.get("answer", "uncertain")).lower()
        verdict = {"yes": "violation", "no": "pass"}.get(word, "uncertain")
        out.append(
            PolicyVerdict(
                rule_id=rule.id,
                stage="qa",
                target=card_id,
                verdict=verdict,
                severity=rule.severity,
                evidence=str(answer.get("evidence") or "no inspector answer"),
                action=action_for(rule.severity, verdict, "qa", hitl),
            )
        )
    return out


def run_detectors(
    card_id: str, ocr: dict, packs: list[PolicyPack], hitl: Hitl
) -> list[PolicyVerdict]:
    """`ocr` is one entry of `cardforge.evaluate.ocr.recognize`; unavailable OCR is uncertain."""
    out = []
    for pack in packs:
        for rule in pack.rules:
            det = rule.detector
            if det is None or "qa" not in rule.stages:
                continue
            if ocr.get("status") != "ok":
                verdict, evidence = "uncertain", f"OCR unavailable: {ocr.get('reason') or ''}"
            else:
                words = [
                    o["text"]
                    for o in ocr.get("observations", [])
                    if float(o.get("confidence", 0)) >= det.min_confidence
                ]
                if det.kind == "ocr_script":
                    found = [w for w in words if _SCRIPTS[det.script].search(w)]
                else:
                    found = [w for w in words if any(d in w for d in det.digits)]
                verdict = "violation" if found else "pass"
                evidence = f"OCR {det.kind}: {found[:5]}" if found else "OCR: no match"
            out.append(
                PolicyVerdict(
                    rule_id=rule.id,
                    stage="qa",
                    target=card_id,
                    verdict=verdict,
                    severity=rule.severity,
                    evidence=evidence,
                    action=action_for(rule.severity, verdict, "qa", hitl),
                )
            )
    return out
