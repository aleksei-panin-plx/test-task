"""Pydantic contracts from Technical Architecture §5."""

from cardforge.contracts.generation import (
    BackendCaps,
    CardEvaluation,
    GenerationAttempt,
    JudgeOutcome,
    PromptBundle,
    Reference,
    RenderParams,
    RenderResult,
    SetEvaluation,
)
from cardforge.contracts.policy import (
    PolicyDetector,
    PolicyMatch,
    PolicyPack,
    PolicyRule,
    PolicyVerdict,
)
from cardforge.contracts.request import (
    Background,
    Budget,
    CardDraft,
    CardSpec,
    CollectionRequest,
    Conflict,
    Hitl,
    PlanDraft,
    SafeZone,
    SetPlan,
)
from cardforge.contracts.run import CardState, Event, IpCheck, RunManifest

__all__ = [
    "BackendCaps",
    "Background",
    "Budget",
    "CardDraft",
    "CardEvaluation",
    "CardState",
    "CardSpec",
    "CollectionRequest",
    "Conflict",
    "Event",
    "GenerationAttempt",
    "Hitl",
    "IpCheck",
    "JudgeOutcome",
    "PlanDraft",
    "PolicyDetector",
    "PolicyMatch",
    "PolicyPack",
    "PolicyRule",
    "PolicyVerdict",
    "PromptBundle",
    "Reference",
    "RenderParams",
    "RenderResult",
    "RunManifest",
    "SafeZone",
    "SetEvaluation",
    "SetPlan",
]
