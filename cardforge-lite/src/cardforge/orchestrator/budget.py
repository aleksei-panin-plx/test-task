"""Run budget: reserve the estimate before each paid call, settle after (TA §7.3, §15.2).

`costs.json` is the ledger. An unsettled reservation (the process died mid-call) keeps
counting at its estimate: the provider may have billed it, so it is never released silently.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from cardforge.orchestrator.store import RunStore, now

LEDGER = "costs.json"


class BudgetLedger:
    def __init__(self, store: RunStore, max_cost_usd: float):
        self.store = store
        self.max_cost_usd = max_cost_usd
        if store.exists(LEDGER):
            data = store.read_json(LEDGER)
            self.calls: list[dict] = data["calls"]
        else:
            self.calls = []
            self._save()

    def committed(self, kind: str | None = None) -> float:
        return sum(
            c["actual_usd"] if c["actual_usd"] is not None else c["estimate_usd"]
            for c in self.calls
            if kind is None or c["kind"] == kind
        )

    def remaining(self) -> float:
        return self.max_cost_usd - self.committed()

    def reserve(
        self,
        estimate_usd: float,
        kind: str,
        card_id: str | None = None,
        *,
        cap_usd: float | None = None,
        **extra: str | None,
    ) -> str | None:
        """Return a reservation id, or None when the estimate does not fit (no call is made).

        `cap_usd` is an extra ceiling for this `kind` alone (a judge's own budget)."""
        with self.store.lock:
            over_kind = cap_usd is not None and self.committed(kind) + estimate_usd > cap_usd + 1e-9
            over_run = self.committed() + estimate_usd > self.max_cost_usd + 1e-9
            # Free calls never touch the ceiling; a zero ceiling still allows them.
            if estimate_usd > 0 and (over_run or over_kind):
                self.store.emit(
                    "budget_warning",
                    {
                        "kind": kind,
                        "estimate_usd": estimate_usd,
                        "remaining_usd": round(self.remaining(), 6),
                        "max_cost_usd": self.max_cost_usd,
                        "cap_usd": cap_usd,
                        "reason": "judge_budget_exhausted" if over_kind else "budget_exhausted",
                    },
                    card_id=card_id,
                )
                return None
            rid = uuid.uuid4().hex[:12]
            self.calls.append(
                {
                    "id": rid,
                    "kind": kind,
                    "card_id": card_id,
                    **extra,
                    "estimate_usd": estimate_usd,
                    "actual_usd": None,
                    "reserved_at": now(),
                }
            )
            self._save()
            return rid

    def settle(self, rid: str, actual_usd: float, duration_s: float | None = None) -> None:
        with self.store.lock:
            call = next(c for c in self.calls if c["id"] == rid)
            call["actual_usd"] = actual_usd
            call["duration_s"] = duration_s
            self._save()

    def totals(self) -> dict:
        by_kind: dict[str, float] = defaultdict(float)
        by_variant: dict[str, float] = defaultdict(float)
        by_card: dict[str, float] = defaultdict(float)
        for c in self.calls:
            cost = c["actual_usd"] if c["actual_usd"] is not None else c["estimate_usd"]
            by_kind[c["kind"]] += cost
            if c.get("variant_id"):
                by_variant[c["variant_id"]] += cost
            if c.get("card_id"):
                by_card[c["card_id"]] += cost
        return {
            "committed_usd": round(self.committed(), 6),
            "max_cost_usd": self.max_cost_usd,
            "unsettled": sum(c["actual_usd"] is None for c in self.calls),
            "by_kind": dict(by_kind),
            "by_variant": dict(by_variant),
            "by_card": dict(by_card),
        }

    def _save(self) -> None:
        self.store.write(LEDGER, {"max_cost_usd": self.max_cost_usd, "calls": self.calls})
