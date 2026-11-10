"""Strict contracts for the seven-axis judge and calibration ground truth."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Criterion(Strict):
    passed: bool
    note: str = Field(min_length=1)


class Rubric(Strict):
    object_match: Criterion
    category: Criterion
    style: Criterion
    thumbnail_readability: Criterion
    composition: Criterion
    artifacts: Criterion
    theme: Criterion


class JudgeAnswer(Strict):
    item_id: str
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    criteria: Rubric
    style_uncertain: bool
    issues: list[str]
    fix_instruction: str | None

    @model_validator(mode="after")
    def actionable(self) -> JudgeAnswer:
        if any(not c.passed for c in self.criteria.__dict__.values()) and not self.fix_instruction:
            raise ValueError("failed criteria require a concrete fix_instruction")
        return self

    @property
    def decision(self) -> str:
        """Legacy projection retained so frozen imports remain reproducible."""
        return self.decision_for("flag-first")

    def decision_for(self, policy: str) -> str:
        if policy not in ("flag-first", "definite-failure-first-v1"):
            raise ValueError("unknown judge decision policy")
        if self.style_uncertain:
            if policy == "definite-failure-first-v1":
                definite = sum(
                    not criterion.passed
                    for name, criterion in self.criteria.__dict__.items()
                    if name != "style"
                )
                if definite:
                    return "fix" if definite <= 2 else "reject"
            return "flag"
        failures = sum(not c.passed for c in self.criteria.__dict__.values())
        return "accept" if not failures else "fix" if failures <= 2 else "reject"


class JudgeIdentity(Strict):
    provider: Literal["host-agent", "gemini"]
    host: str = Field(min_length=1)
    model: str = Field(min_length=1)
    family: str = Field(min_length=1)
    role: Literal["working", "independent"]


class Label(Strict):
    item_id: str
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cohort: Literal["real", "foreign", "borderline"]
    accepted: bool
    source: Literal["reference", "human", "agent"]
    reviewer: str = Field(min_length=1)
    note: str

    @model_validator(mode="after")
    def provenance(self) -> Label:
        if self.cohort == "borderline" and self.source != "human":
            raise ValueError("borderline ground truth must be human-labelled")
        if self.source == "reference" and self.cohort != "real":
            raise ValueError("reference labels are positive source controls only")
        if self.cohort == "real" and (not self.accepted or self.source == "agent"):
            raise ValueError("real cohort is the positive reference control")
        return self
