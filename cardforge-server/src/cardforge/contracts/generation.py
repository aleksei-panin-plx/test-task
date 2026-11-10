"""Generation and evaluation contracts (TA §5.3, §10.1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field

from cardforge.contracts.policy import PolicyVerdict, Strict

AttemptStatus = Literal["ok", "blocked_by_provider", "error", "timeout"]
Decision = Literal["accept", "fix", "reject", "flag"]


class Reference(Strict):
    id: str
    role: Literal["style", "category", "material", "background", "swatch"]
    path: str


class RenderParams(Strict):
    width: int
    height: int
    steps: int | None = None
    guidance: float | None = None
    seed: int
    quantize: int | None = None
    loras: list[dict[str, Any]] = Field(default_factory=list)
    strength: float | None = None


class PromptBundle(Strict):
    backend: str
    model_id: str
    prompt_text: str
    references: list[Reference] = Field(default_factory=list)
    params: RenderParams
    template_version: str
    hash: str = ""

    def compute_hash(self) -> str:
        """sha256 of everything except the hash itself — the idempotency key (TA §7.3)."""
        body = self.model_dump(mode="json", exclude={"hash"})
        return hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def sealed(self) -> PromptBundle:
        return self.model_copy(update={"hash": self.compute_hash()})


class BackendCaps(Strict):
    max_refs: int
    supports_seed: bool
    supports_negative: bool
    supports_img2img: bool
    supports_edit: bool
    supports_lora: bool
    native_sizes: list[tuple[int, int]] | None = None
    local: bool
    license_class: Literal["commercial_api", "open", "community", "research", "test"]


class RenderResult(Strict):
    status: AttemptStatus
    image_png: bytes | None = None
    cost_usd: float = 0.0
    model_revision: str = ""
    provider_request_id: str | None = None
    peak_memory_gb: float | None = None
    error: str | None = None


class GenerationAttempt(Strict):
    attempt_no: int
    card_id: str
    bundle_hash: str
    backend: str
    model_id: str
    model_revision: str
    started_at: str
    finished_at: str
    duration_s: float
    peak_memory_gb: float | None = None
    cost_usd: float
    provider_request_id: str | None = None
    status: AttemptStatus
    raw_path: str
    error: str | None = None
    fix_instruction: str | None = None


class JudgeOutcome(Strict):
    criteria: dict[str, dict[str, Any]] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    fix_instruction: str | None = None
    judge_model: str
    decision: Decision
    decision_policy: str


class CardEvaluation(Strict):
    hard_checks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    policy_verdicts: list[PolicyVerdict] = Field(default_factory=list)
    judge: JudgeOutcome | None = None
    style_distance: dict[str, float] | None = None
    decision: Decision
    decided_by: Literal["auto", "human"] = "auto"
    reasons: list[str] = Field(default_factory=list)
    fix_instruction: str | None = None
    # Judge panel: answers of the non-deciding judges, logged for comparison only.
    shadow_judges: dict[str, JudgeOutcome | str] = Field(default_factory=dict)
    # Candidate mode: per-candidate checks and verdicts, the rule and its outcome.
    candidates: dict[str, Any] = Field(default_factory=dict)
    chosen_candidate: int | None = None
    rule: dict[str, Any] | None = None
    # Phase 1 of the judge autonomy strategy: the fixes this evaluation asks for
    # ({source, codes, text}; codes from prompts/render/fixes.toml) and the targeted
    # verification of earlier fixes on this attempt ({code: {answer, evidence, resolved}}).
    fixes: list[dict[str, Any]] = Field(default_factory=list)
    fix_checks: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SetEvaluation(Strict):
    variant_id: str
    category_distribution: dict[str, int]
    color_rhythm: dict[str, Any]
    scale_spread: list[str]
    type_spread: list[str]
    duplicates: list[list[str]]
    style_consistency: dict[str, Any] | None = None
    sheet_review: dict[str, Any] | None = None
    decision: Literal["pass", "warn", "fail"]
    notes: list[str] = Field(default_factory=list)
