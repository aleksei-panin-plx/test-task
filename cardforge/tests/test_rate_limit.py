"""Debt T2 (stage 7, 2026-11-10): parallel runs share machine-wide provider slots, a 429 backs
off longer (Retry-After honoured) and an answered 429 / 5xx costs nothing in the run budget."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from cardforge.contracts.request import Budget, CollectionRequest, Hitl
from cardforge.orchestrator import service
from cardforge.orchestrator.execution import (
    Limiter,
    SharedSlots,
    TransientError,
    retry_delay,
    with_retries,
)
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace
from cardforge.render.fake import FakeBackend

ROOT = Path(__file__).resolve().parents[1]


def test_429_backs_off_longer_and_honours_retry_after():
    e429 = TransientError("429", status=429)
    e503 = TransientError("503", status=503)
    no_jitter = lambda: 0.0  # noqa: E731
    assert retry_delay(e503, 0, 1.0, no_jitter) == 1.0
    assert retry_delay(e429, 0, 1.0, no_jitter) == 5.0
    assert retry_delay(e429, 3, 1.0, no_jitter) == 40.0
    assert retry_delay(e429, 6, 1.0, no_jitter) == 60.0  # capped
    after = TransientError("429", status=429, retry_after_s=30)
    assert retry_delay(after, 0, 1.0, no_jitter) == 30.0
    assert 5.0 <= retry_delay(e429, 0, 1.0) <= 6.25  # jitter only for 429
    assert e429.answered and not TransientError("t", timeout=True).answered


def test_with_retries_uses_rate_limit_backoff():
    calls, delays = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("429", status=429, retry_after_s=12)
        return "ok"

    assert with_retries(flaky, sleep=delays.append) == "ok"
    assert len(delays) == 2 and delays[0] >= 12


def _hold(folder: str, slots: int, out, hold_s: float) -> None:
    with SharedSlots(Path(folder), slots, poll_s=0.01).slot() as i:
        out.put(("in", time.monotonic(), i))
        time.sleep(hold_s)
        out.put(("out", time.monotonic(), i))


def test_shared_slots_limit_parallel_processes(tmp_path):
    """Four processes, two slots: at most two hold a slot at any moment."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_hold, args=(str(tmp_path), 2, q, 0.4)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    events = sorted((q.get(timeout=5) for _ in range(8)), key=lambda e: e[1])
    inside = peak = 0
    for kind, _, _ in events:
        inside += 1 if kind == "in" else -1
        peak = max(peak, inside)
    assert peak == 2


def test_limiter_takes_a_shared_slot(tmp_path):
    shared = SharedSlots(tmp_path, 1, poll_s=0.01)
    limiter = Limiter(local=False, concurrency=4, shared=shared)
    with limiter.slot():
        def give_up(_s):
            raise TimeoutError

        other = SharedSlots(tmp_path, 1, poll_s=0.01, sleep=give_up)
        with pytest.raises(TimeoutError):  # the only slot is taken: the other waits
            with other.slot():
                pass
    with other.slot():  # released
        pass


class Throttled(FakeBackend):
    """Every render answers 429 (no image, nothing billed)."""

    def render(self, bundle, timeout_s):
        raise TransientError("HTTP 429", status=429)


def test_answered_429_costs_nothing_in_the_run_budget(tmp_path):
    ws = Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))
    request = CollectionRequest(
        request_id="t", collection_theme="Кино", set_theme="Вестерн", producer_wishes_raw="",
        render_profile="fake", hitl=Hitl(g1="none", g2="none"), budget=Budget(max_cost_usd=5.0),
    )  # fmt: skip
    rid = service.create_run(ws, request, llm="fake", render="fake", judge="fake",
                             shadow_judges=[], run_id="t2")  # fmt: skip
    store = RunStore(ws.run_dir(rid))
    runtime = service.build_runtime(ws, store.manifest())
    runtime.backend = Throttled(cost_per_image_usd=0.08)
    runtime.render_estimate_usd = 0.08
    for slot in runtime.slots.values():
        slot.estimate_usd = 0.08
    runtime.sleep = lambda s: None
    service.advance(ws, rid, runtime)
    renders = [c for c in store.read_json("costs.json")["calls"] if c["kind"] == "render"]
    assert renders and all(c["actual_usd"] == 0.0 for c in renders)
    finished = [e.payload for e in store.events() if e.type == "render_finished"]
    assert finished and all(p["cost_usd"] == 0.0 for p in finished)
