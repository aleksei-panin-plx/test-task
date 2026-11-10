"""Run and card state machines (TA §7.1). Every change is a `state_changed` event."""

from __future__ import annotations

from cardforge.contracts.run import CardState, RunManifest
from cardforge.orchestrator.store import RunStore

RUN_TERMINAL = {"completed", "partial", "failed"}
# `awaiting_agent` (TA §8.4) may interrupt any step that needs an LLM; `planning_repair`
# is the stage-3 repair loop; `planning_failed` ends a run whose plan never validated.
RUN_TRANSITIONS: dict[str, set[str]] = {
    "created": {"normalized", "awaiting_agent", "failed"},
    "normalized": {"planned", "planning_repair", "awaiting_agent", "failed"},
    "planning_repair": {"planned", "planning_repair", "awaiting_agent", "failed"},
    "planned": {"awaiting_g1", "rendering", "awaiting_agent", "failed"},
    "awaiting_g1": {"awaiting_g1", "planning_repair", "awaiting_agent", "rendering", "failed"},
    "rendering": {"awaiting_g2", "awaiting_agent", "exported", "failed"},
    "awaiting_g2": {"rendering", "exported", "awaiting_agent", "failed"},
    "exported": {"completed", "partial"},
    # G3 (2026-11-09): returning an auto-passed card reopens a finished run for its G2 review.
    "completed": {"rendering"},
    # `reopen`: a partial run continues when its cause is gone (budget, pending fixes).
    "partial": {"rendering"},
    "failed": set(),
}
CARD_TERMINAL = {"accepted", "rejected", "failed"}
CARD_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"spec_ready", "rejected"},
    "spec_ready": {"rendering", "rejected"},
    "rendering": {"evaluating", "rendering", "flagged", "failed"},
    "evaluating": {"accepted", "needs_fix", "rejected", "flagged"},
    "needs_fix": {"rendering", "rejected"},
    "flagged": {"accepted", "rejected", "needs_fix"},
    # Only a human reopens an accepted card: at G2 in mode `all`, or by returning an
    # auto-passed card at G3 (→ flagged, reviewed at G2; 2026-11-09).
    "accepted": {"rejected", "needs_fix", "flagged"},
    # Only a person brings a rejected card back to G2 with its last image (`card_reopen`).
    "rejected": {"flagged"},
    # An operator may retry a backend failure after fixing its cause (`retry_failed`).
    "failed": {"needs_fix"},
}


class TransitionError(RuntimeError):
    pass


def set_run_state(store: RunStore, state: str, reason: str | None = None) -> RunManifest:
    """Move the run; `awaiting_agent` returns to whatever step asked for the agent."""

    def change(m: RunManifest) -> None:
        if m.state == state and reason is None:
            return
        allowed = RUN_TRANSITIONS.get(m.state, set())
        if m.state != state and m.state != "awaiting_agent" and state not in allowed:
            raise TransitionError(f"run: {m.state} → {state} is not allowed")
        store.emit("state_changed", {"from": m.state, "to": state, "reason": reason})
        m.state = state
        # Only a final state keeps its reason in the manifest; others live in the journal.
        m.status_reason = reason if state in RUN_TERMINAL else None

    return store.update_manifest(change)


def set_card_state(store: RunStore, card_id: str, state: str, reason: str | None = None) -> None:
    def change(m: RunManifest) -> None:
        card = m.cards[card_id]
        if card.state == state:
            return
        if state not in CARD_TRANSITIONS[card.state]:
            raise TransitionError(f"{card_id}: {card.state} → {state} is not allowed")
        store.emit(
            "state_changed",
            {"from": card.state, "to": state, "reason": reason},
            card_id=card_id,
            variant_id=card.variant_id,
        )
        card.state = state
        card.reason = reason

    store.update_manifest(change)


def register_cards(store: RunStore, cards: dict[str, str]) -> None:
    """`cards` maps card_id → variant_id; already known cards keep their state."""

    def change(m: RunManifest) -> None:
        for card_id, variant in cards.items():
            m.cards.setdefault(card_id, CardState(variant_id=variant))

    store.update_manifest(change)
