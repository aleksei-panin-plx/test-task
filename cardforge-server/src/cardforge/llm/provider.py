"""`LLMProvider`: every runtime LLM/VLM call goes through `ask` (TA §8.4).

A task is a JSON object with `step`, `schema_version`, `instruction`, inputs and
`answer_schema`; `seal` adds `task_sha256`. An answer is valid only for the exact task it
echoes, so a repaired plan never reuses an answer written for the old one. A provider
either answers now (`LLMReply`) or asks the run to wait (`Pending` → `awaiting_agent`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from cardforge.orchestrator.store import RunStore

ENVELOPE = ("task_sha256", "provider", "model", "schema_version")


def task_hash(task: dict) -> str:
    body = {k: v for k, v in task.items() if k != "task_sha256"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def seal(task: dict) -> dict:
    return {**task, "task_sha256": task_hash(task)}


@dataclass
class LLMReply:
    data: dict[str, Any]
    provider: str
    model: str
    cost_usd: float = 0.0


@dataclass
class Pending:
    key: str
    task_path: str
    reason: str = "no answer yet"
    extra: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    def estimate_cost(self, task: dict) -> float: ...

    def ask(self, store: RunStore, key: str, task: dict) -> LLMReply | Pending: ...


class StaleAnswer(ValueError):
    pass


def unwrap(answer: dict, task: dict) -> LLMReply:
    """Check the envelope and return the payload without it."""
    if answer.get("task_sha256") != task["task_sha256"]:
        raise StaleAnswer("answer was written for a different task")
    for key in ("provider", "model"):
        if not str(answer.get(key) or "").strip():
            raise ValueError(f"answer must name its {key}")
    return LLMReply(
        data={k: v for k, v in answer.items() if k not in ENVELOPE},
        provider=str(answer["provider"]),
        model=str(answer["model"]),
    )


class HostAgentProvider:
    """Claude Code or Codex answers task files; the run waits in `awaiting_agent`."""

    name = "host-agent"

    def estimate_cost(self, task: dict) -> float:
        return 0.0  # paid by the host agent's own subscription, not the run budget

    def ask(self, store: RunStore, key: str, task: dict) -> LLMReply | Pending:
        task_rel, answer_rel = f"tasks/{key}.task.json", f"tasks/{key}.answer.json"
        if not store.exists(task_rel) or store.read_json(task_rel) != task:
            store.write(task_rel, task)
        if not store.exists(answer_rel):
            return Pending(key, task_rel)
        try:
            return unwrap(store.read_json(answer_rel), task)
        except StaleAnswer:
            return Pending(key, task_rel, "stale answer", {"task_sha256": task["task_sha256"]})


class NoProvider:
    """Placeholder for the `none` judge profile: never answers, never costs."""

    name = "none"

    def estimate_cost(self, task: dict) -> float:
        return 0.0

    def ask(self, store: RunStore, key: str, task: dict) -> LLMReply | Pending:
        raise RuntimeError("the `none` profile has no LLM; callers must not ask it")
