"""Policy contracts (TA §5.4)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["block", "review", "advise"]
Stage = Literal["plan", "prompt", "qa"]
Target = Literal["object", "background", "text", "theme", "character"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyMatch(Strict):
    keywords: list[str] = Field(default_factory=list)
    semantic: str


class PolicyDetector(Strict):
    """Deterministic QA detector; thresholds live in the pack, not in code (TA §9.4)."""

    kind: Literal["ocr_script", "ocr_digit"]
    script: Literal["han", "latin"] | None = None
    digits: list[str] = Field(default_factory=list)
    min_confidence: float = Field(default=0.3, ge=0, le=1)


class PolicyRule(Strict):
    id: str
    title: str
    severity: Severity
    stages: list[Stage] = Field(min_length=1)
    applies_to: list[Target] = Field(min_length=1)
    match: PolicyMatch
    prompt_constraint: str | None = None
    qa_question: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    rationale: str
    sources: list[str] = Field(default_factory=list)
    detector: PolicyDetector | None = None


class PolicyPack(Strict):
    policy_id: str
    policy_kind: Literal["ip", "culture", "topic"]
    region: str | None = None
    policy_version: str
    includes: list[str] = Field(default_factory=list)
    verified: date | None = None
    review_after: date | None = None
    rules: list[PolicyRule]
    file_sha256: str
    path: str
    checklist: list[str] = Field(default_factory=list)

    @property
    def mandatory(self) -> bool:
        return self.policy_kind == "ip"


class PolicyVerdict(Strict):
    rule_id: str
    stage: Stage
    target: str
    verdict: Literal["pass", "violation", "uncertain"]
    severity: Severity
    evidence: str
    action: str
    decided_by: Literal["auto", "human"] = "auto"
