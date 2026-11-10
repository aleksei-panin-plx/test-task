"""Persistent request reservations: interrupted or ambiguous API calls retain their ceiling."""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from cardforge.evaluate.common import dump


def token_cost(
    input_tokens: int, output_tokens: int, input_rate: float, output_rate: float
) -> Decimal:
    if min(input_tokens, output_tokens, input_rate, output_rate) < 0:
        raise ValueError("negative token count or price")
    return (
        Decimal(input_tokens) * Decimal(str(input_rate))
        + Decimal(output_tokens) * Decimal(str(output_rate))
    ) / Decimal(1_000_000)


class Ledger:
    def __init__(self, path: Path, cap_usd: float, authorized_max_usd: float = 8):
        # Gemini (default) and OpenRouter are separate user authorizations and ledgers.
        self.path = path
        self.cap = Decimal(str(cap_usd))
        limit = Decimal(str(authorized_max_usd))
        if not self.cap.is_finite() or not 0 < self.cap <= limit:
            raise ValueError(f"current authorization covers a positive ceiling up to ${limit}")

    @contextmanager
    def locked(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "version": 1,
                    "entries": {},
                    "scope": "Conservative project API accounting, "
                    "not a provider invoice; excludes unrelated account usage.",
                }
            )
            yield data

    def reserve(self, request_id: str, ceiling: Decimal, metadata: dict):
        if not ceiling.is_finite() or ceiling <= 0:
            raise ValueError("positive finite reservation required")
        with self.locked() as data:
            if request_id in data["entries"]:
                raise ValueError(
                    "request already accounted for; use saved response, never retry blindly"
                )
            total = sum(
                (Decimal(e["charged_upper_usd"]) for e in data["entries"].values()), Decimal(0)
            )
            if total + ceiling > self.cap:
                raise ValueError("project API budget exhausted; request not sent")
            data["entries"][request_id] = {
                **metadata,
                "status": "reserved",
                "reserved_usd": str(ceiling),
                "charged_upper_usd": str(ceiling),
            }
            dump(self.path, data)

    def settle(self, request_id: str, upper_cost: Decimal, usage: dict):
        with self.locked() as data:
            entry = data["entries"][request_id]
            if (
                entry["status"] == "usage_recorded"
                and Decimal(entry["charged_upper_usd"]) != upper_cost
            ):
                raise ValueError("recorded usage cannot be rewritten")
            if not upper_cost.is_finite() or not 0 <= upper_cost <= Decimal(entry["reserved_usd"]):
                raise ValueError("usage outside reserved bound; keep reservation and stop")
            entry.update(status="usage_recorded", charged_upper_usd=str(upper_cost), usage=usage)
            dump(self.path, data)

    def summary(self) -> dict:
        with self.locked() as data:
            total = sum(
                (Decimal(e["charged_upper_usd"]) for e in data["entries"].values()), Decimal(0)
            )
            return {
                "cap_usd": str(self.cap),
                "accounted_upper_usd": str(total),
                "remaining_usd": str(self.cap - total),
                "requests": len(data["entries"]),
                "unsettled": sum(e["status"] == "reserved" for e in data["entries"].values()),
                "scope": data["scope"],
            }
