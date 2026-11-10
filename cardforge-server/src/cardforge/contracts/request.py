"""Brief and plan contracts (TA §5.1, §5.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from cardforge.contracts.policy import PolicyVerdict, Strict

GateMode = Literal["all", "flagged", "none"]


class Conflict(Strict):
    kind: Literal["ip", "culture", "topic", "style"]
    wish: str
    rule_id: str | None = None
    resolution: str


class Hitl(Strict):
    g1: GateMode = "all"
    g2: GateMode = "flagged"
    # TA §9.5: without a human, review findings are conservatively replaced.
    review_policy: Literal["replace", "accept_with_note"] = "replace"


class Budget(Strict):
    max_cost_usd: float = Field(default=0.0, ge=0)
    max_attempts_per_card: int = Field(default=3, ge=1)
    max_fix_rounds: int = Field(default=2, ge=0, le=2)


class CollectionRequest(Strict):
    request_id: str
    collection_theme: str
    set_theme: str
    producer_wishes_raw: str
    constraints: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    # `variants`: 3–5 alternative plans of one set (stages 4–9). `collection` (user 2026-11-10):
    # one plan of the whole collection — `sets_total` set themes; sets 1..min(sets_total, 15) get
    # one card list each (object cards only, gold slots by the reference pattern); set 16 = theme.
    plan_mode: Literal["variants", "collection"] = "variants"
    sets_total: int = Field(default=16, ge=1, le=16)
    variants_count: int = Field(default=3, ge=1, le=15)
    cards_per_set: Literal[10] = 10
    category_counts: dict[str, int] | None = None
    markets: list[str] = Field(default_factory=list)
    policy_packs: list[str] = Field(default_factory=list)
    render_profile: str = "local-klein4b"
    hitl: Hitl = Field(default_factory=Hitl)
    budget: Budget = Field(default_factory=Budget)
    seed: int = 0


    @model_validator(mode="after")
    def mode_counts(self) -> CollectionRequest:
        # 3–5 alternative plans; «1 main + N alternatives» on a producer's request: up to 10.
        if self.plan_mode == "variants" and not 2 <= self.variants_count <= 10:
            raise ValueError("variants mode: variants_count must be 2–10")
        if self.plan_mode == "collection" and self.variants_count != min(self.sets_total, 15):
            raise ValueError("collection mode: variants_count = drawn sets = min(sets_total, 15)")
        return self


class Background(Strict):
    motif: str
    color: str


class SafeZone(Strict):
    top_pct: float
    bottom_pct: float
    side_pct: float


class CardDraft(Strict):
    """Fields a planner (Gemini or host agent) writes; the core fills the rest of CardSpec."""

    object_name: str
    # English generator/judge description ("brass spyglass telescope, extended"); optional.
    object_description: str | None = None
    theme_rationale: str
    category: Literal[1, 2, 3, 4]
    scale_class: Literal["handheld", "tabletop", "furniture", "vehicle", "structure"]
    object_type: Literal[
        "tool", "food", "clothing", "vehicle", "structure", "decor", "device", "plant", "other"
    ]
    composition: str
    background: Background | None = None
    surface: str | None = None
    environment: str | None = None
    materials: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    must_not_have: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def category_fields(self) -> CardDraft:
        if self.category == 1 and self.surface:
            raise ValueError(f"{self.object_name}: category 1 has no surface")
        if self.category in (2, 3) and not self.surface:
            raise ValueError(f"{self.object_name}: categories 2-3 need a surface")
        if self.category == 4 and not self.environment:
            raise ValueError(f"{self.object_name}: category 4 needs an environment")
        return self


class CardSpec(CardDraft):
    card_id: str
    safe_zone: SafeZone
    reference_roles: dict[str, list[str]] = Field(default_factory=dict)
    style_profile_id: str
    policy_flags: list[PolicyVerdict] = Field(default_factory=list)
    seed: int


class PlanDraft(Strict):
    variant_id: str
    title: str
    concept: str
    # Collection mode: 10 − gold_slots object cards; gold positions come last and stay empty.
    cards: list[CardDraft] = Field(min_length=1, max_length=10)
    set_index: int | None = Field(default=None, ge=1, le=16)
    gold_slots: int = Field(default=0, ge=0, le=10)
    color_plan: list[str] = Field(default_factory=list)
    diversity_notes: str = ""
    policy_notes: list[str] = Field(default_factory=list)


class SetPlan(Strict):
    variant_id: str
    title: str
    concept: str
    cards: list[CardSpec] = Field(min_length=1, max_length=10)
    set_index: int | None = Field(default=None, ge=1, le=16)
    gold_slots: int = Field(default=0, ge=0, le=10)
    color_plan: list[str] = Field(default_factory=list)
    diversity_notes: str = ""
    policy_notes: list[str] = Field(default_factory=list)
