"""One budgeted, journaled LLM call (TA §7.3, §7.4)."""

from __future__ import annotations

import time

from cardforge.llm.provider import LLMProvider, LLMReply, Pending, seal
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.store import RunStore


class BudgetExhausted(RuntimeError):
    pass


def ask(
    store: RunStore,
    ledger: BudgetLedger,
    provider: LLMProvider,
    key: str,
    task: dict,
    *,
    card_id: str | None = None,
    variant_id: str | None = None,
    kind: str | None = None,
    cap_usd: float | None = None,
) -> LLMReply | Pending:
    task = task if "task_sha256" in task else seal(task)
    estimate = provider.estimate_cost(task)
    kind = kind or f"llm:{task['step']}"
    rid = None
    if estimate > 0:
        rid = ledger.reserve(estimate, kind, card_id, cap_usd=cap_usd, variant_id=variant_id)
        if rid is None:
            raise BudgetExhausted(key)
    started = time.monotonic()
    try:
        reply = provider.ask(store, key, task)
    except BaseException:
        if rid:
            ledger.settle(rid, estimate)  # unknown outcome: keep the conservative estimate
        raise
    duration = round(time.monotonic() - started, 3)
    if isinstance(reply, Pending):
        if rid:
            ledger.settle(rid, 0.0)
        return reply
    # Free answers (host agent, fake) are still listed so costs.json shows every call.
    rid = rid or ledger.reserve(0.0, kind, card_id, variant_id=variant_id)
    ledger.settle(rid, reply.cost_usd, duration)
    store.emit(
        "llm_call",
        {
            "step": task["step"],
            "key": key,
            "provider": reply.provider,
            "model": reply.model,
            "cost_usd": reply.cost_usd,
            "duration_s": duration,
            "task_sha256": task["task_sha256"],
        },
        card_id=card_id,
        variant_id=variant_id,
    )
    return reply
