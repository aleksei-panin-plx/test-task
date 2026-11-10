"""Run journal and manifest contracts (TA §5.5, §7.4)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from cardforge.contracts.policy import Strict

EventType = Literal[
    "state_changed",
    "llm_call",
    "render_started",
    "render_finished",
    "check_result",
    "policy_verdict",
    "judge_result",
    "human_decision",
    "fix_recorded",
    "budget_warning",
    "error",
    "edit_pass",  # 2026-11-06: one image edit instead of a redraw (config/edit_pass.toml)
    # 2026-11-08 (shadow-01): emitted by planner.planner since H4 / Q1 but missing here, so the
    # first real plan with a material rewrite or an over-budget variant failed validation.
    "material_rules",  # config/material_rules.toml rewrites of object_description
    "material_budget",  # config/material_budget.toml: variants over the bare-metal budget
    # 2026-11-09 (autonomy-readiness §8): a card moved to its profile's reserve generator (G1),
    # the outcome of a redraw caused by a hard check (G7 / §4.4) and the shadow auto-pass zone.
    "render_fallback",
    "hard_check_redraw",
    "auto_pass_shadow",
    # 2026-11-09 («Да, все три»): real auto-pass of GPT cards, a G3 return of one, the breaker.
    "auto_pass",
    "auto_pass_returned",
    "auto_pass_disabled",
    # 2026-11-09 («реализуй»): a person brings a rejected / accepted card back to G2, and the
    # variant's confirmed ip_check goes back to pending because its card set changed.
    "card_reopened",
    "ip_check_reset",
    # 2026-11-10 (stage 7 plugin): designer delivery written; cards regenerated on request;
    # a background worker started or finished a run step.
    "delivery_written",
    "cards_regenerated",
    "worker",
]


class Event(Strict):
    event_id: str
    ts: str
    run_id: str
    type: EventType
    card_id: str | None = None
    variant_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class IpCheck(Strict):
    """TA §9.8: `confirmed` only with a reference to a human_decision event."""

    status: Literal["pending", "confirmed"] = "pending"
    event_id: str | None = None


class CardState(Strict):
    """Card state machine position and counters (TA §7.1)."""

    variant_id: str
    state: str = "planned"
    attempts: int = 0
    fix_rounds: int = 0
    accepted_attempt: int | None = None
    first_attempt_accepted: bool | None = None
    # Candidate mode: which of the N candidates of the accepted attempt was chosen.
    accepted_candidate: int | None = None
    reason: str | None = None


class RunManifest(Strict):
    run_id: str
    created_at: str
    state: str = "created"
    # Terminal reason for `partial`/`failed` (`budget_exhausted`, `planning_failed`, ...).
    status_reason: str | None = None
    # TA §13.4: who started the run, through which client, under which deployment profile.
    owner: str = "local"
    client: Literal["cli", "mcp", "web"] = "cli"
    deployment: Literal["local", "server"] = "local"
    # TA §7.3: one render profile per run, fixed at start; the judge is a pluggable profile.
    render_profile: str | None = None
    # Several models (user, 2026-11-03): each set variant gets exactly one profile (TA §7.3).
    variant_profiles: dict[str, str] = Field(default_factory=dict)
    render_profiles: list[str] = Field(default_factory=list)
    # G1 (user 2026-11-09): main profile → reserve profile; a card switches to the reserve
    # after `fallback_after_failures` failed attempts in a row (counters["fallback"]).
    render_fallbacks: dict[str, str] = Field(default_factory=dict)
    fallback_after_failures: int = 2
    judge_profile: str | None = None
    # Deciding judges and the rule that combines them; `shadow_judges` are only logged.
    deciders: list[str] = Field(default_factory=list)
    decision_rule: str | None = None
    candidates_per_card: int = 1
    # Test limit (user, 2026-11-03): render only the first N approved variants; the rest of
    # the plan stays planned-only and the report says so.
    render_variant_limit: int | None = None
    # Model comparison: the first variant cloned once per render profile (`v01`, `v01m2`, ...).
    compare_models: bool = False
    compare_variants: list[str] = Field(default_factory=list)
    shadow_judges: list[str] = Field(default_factory=list)
    runtime_options: dict[str, Any] = Field(default_factory=dict)
    cards: dict[str, CardState] = Field(default_factory=dict)
    gate_modes: dict[str, str] = Field(default_factory=dict)
    versions: dict[str, Any] = Field(default_factory=dict)
    llm_provider: dict[str, str] = Field(default_factory=dict)
    ip_check: dict[str, IpCheck] = Field(default_factory=dict)
    counters: dict[str, Any] = Field(default_factory=dict)
