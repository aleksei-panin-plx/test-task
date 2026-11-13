"""Brief and plan contracts (TA §5.1, §5.2), CardForge Light: G1 only (plan and content tables)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from cardforge.contracts.policy import Strict

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
    # Light: one plan of the whole collection (user 2026-11-10) — `sets_total` set themes; sets
    # 1..min(sets_total, 15) get one card list each (object cards only, gold slots by the
    # reference pattern); set 16 = theme only. The full version's `variants` mode is not here.
    plan_mode: Literal["collection"] = "collection"
    sets_total: int = Field(default=16, ge=1, le=16)
    variants_count: int | None = Field(default=None, ge=1, le=15)  # drawn sets; filled below
    cards_per_set: Literal[10] = 10
    category_counts: dict[str, int] | None = None
    markets: list[str] = Field(default_factory=list)
    policy_packs: list[str] = Field(default_factory=list)
    hitl: Hitl = Field(default_factory=Hitl)
    budget: Budget = Field(default_factory=Budget)
    seed: int = 0

    @model_validator(mode="after")
    def mode_counts(self) -> CollectionRequest:
        if self.variants_count is None:
            self.variants_count = min(self.sets_total, 15)
        if self.variants_count != min(self.sets_total, 15):
            raise ValueError("collection mode: variants_count = drawn sets = min(sets_total, 15)")
        return self


class Background(Strict):
    motif: str
    color: str


# Empty-environment spellings for categories 1–3 (the table shows «—»).
NO_ENVIRONMENT = {"", "-", "—", "–", "none", "нет"}


class CardDraft(Strict):
    """Fields a planner (the host agent) writes for one object card of the G1 plan."""

    object_name: str
    # Light (2026-11-13): the main object for the content table, in the plan's language:
    # form, proportions and main colour («круглый пузатый чайник, низкий и широкий, красный
    # с кремовой крышкой»). Detail level: prompts/planner/card_rules.md.
    main_object: str = Field(min_length=3)
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
    # Light: the secondary surroundings of a category-4 scene («причал с канатами и чайками»);
    # categories 1–3 have none (empty, shown as «—»).
    environment: str | None = None
    materials: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    must_not_have: list[str] = Field(default_factory=list)

    @field_validator("environment", mode="before")
    @classmethod
    def empty_environment(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().lower() in NO_ENVIRONMENT:
            return None
        return v

    @model_validator(mode="after")
    def category_fields(self) -> CardDraft:
        if self.category == 1 and self.surface:
            raise ValueError(f"{self.object_name}: category 1 has no surface")
        if self.category in (2, 3) and not self.surface:
            raise ValueError(f"{self.object_name}: categories 2-3 need a surface")
        if self.category == 4 and not self.environment:
            raise ValueError(f"{self.object_name}: category 4 needs an environment")
        if self.category in (1, 2, 3) and self.environment:
            raise ValueError(
                f"{self.object_name}: environment is only for category 4 (categories 1-3: empty)"
            )
        return self


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
