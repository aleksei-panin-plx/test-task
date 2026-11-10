# ruff: noqa: E501
"""Job dispatcher (portal plan §6): claim a queued run → advance it in its own process → update
the index → next. `advance` stops by itself at `awaiting_*`; a gate decision or an agent answer
queues the run again (`product.resume` → `core_hooks._enqueue`).

- Concurrency: at most `WORKER_CONCURRENCY` runs at once; the rest wait in the queue (FIFO).
  Inside a run the core parallelises renders per provider (shared slots `runs/_api/slots`).
- Kill switch: while `runs/STOP` exists (or `CARDFORGE_STOP=1`) no job starts; running jobs stop
  after their current call (the core checks the same file). A per-run `STOP` (stop_run) cancels
  that run's queued job.
- Paid lock: each job is `python -m cardforge work <run>` — the core's `product.work`, which
  takes the machine-wide `runs/_api/paid-run.lock` for paid runs. A separate process per job is
  required: the lock is re-entrant per pid, so two paid runs in threads of one process would
  both pass it.
- Crash: on start, `running` jobs whose process is gone are queued again (idempotent advance).

    python -m server.worker        # external dispatcher (CARDFORGE_SERVER_WORKER=external)
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from cardforge.orchestrator import product, service
from cardforge.orchestrator.execution import stop_requested
from cardforge.orchestrator.service import RUN_STOP
from cardforge.orchestrator.store import RunStore, now
from cardforge.orchestrator.workspace import Workspace
from server import usage
from server.settings import Settings
from server.store_index import Index


def _alive(pid: int | None) -> bool:
    try:
        return bool(pid) and psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except psutil.Error:
        return False


def refresh(index: Index, ws: Workspace, run_id: str) -> dict | None:
    """Copy state, awaiting gate and cost of a run folder into the index."""
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("manifest.json"):
        return None
    st = service.status(ws, run_id)
    awaiting = {"awaiting_g1": "g1", "awaiting_g2": "g2", "awaiting_agent": "agent"}.get(st["state"])
    if st["state"] in ("completed", "partial"):
        awaiting = "g3"
    index.update_run(run_id, state=st["state"], awaiting=awaiting, cost_usd=float(st.get("cost_usd") or 0))
    return st


class Dispatcher:
    def __init__(self, settings: Settings, index: Index, ws: Workspace):
        self.s = settings
        self.index = index
        self.ws = ws
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self.running: dict[int, tuple[str, object]] = {}  # job id → (run id, Popen | Thread)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_retention = 0.0

    # ---------------------------------------------------------------- one step

    def recover(self) -> list[int]:
        return self.index.requeue_stale(self.id, _alive)

    def tick(self) -> None:
        self._reap()
        for rid, _ in list(self.running.values()):  # paid calls as they settle (usage + limit)
            usage.sync_worker(self.index, self.ws, rid)
        self._retention()
        if stop_requested(self.ws.runs_dir):
            return  # kill switch: nothing new starts
        while len(self.running) < self.s.worker_concurrency:
            job = self.index.claim(self.id)
            if job is None:
                return
            store = RunStore(self.ws.run_dir(job["run_id"]))
            if not store.exists("manifest.json"):
                self.index.finish(job["id"], "failed", "no run folder")
                continue
            if store.exists(RUN_STOP):
                self.index.finish(job["id"], "cancelled", "run stopped")
                continue
            self._start(job, store)

    def _start(self, job: dict, store: RunStore) -> None:
        rid = job["run_id"]
        # Monthly limit per user: the run's own ceiling is set from the owner's remainder.
        from server.budget_users import apply_cap

        try:
            apply_cap(self.index, self.s, self.ws, rid)
        except Exception as exc:  # never start a run whose ceiling could not be set
            self.index.finish(job["id"], "failed", f"budget cap: {type(exc).__name__}: {exc}")
            return
        if self.s.worker_mode == "inline":
            store.write(product.WORKER, {"pid": os.getpid(), "started": now(), "inline": True, "job": job["id"]})

            def body() -> None:
                try:
                    product.work(self.ws, rid)
                except Exception:  # journaled by product.work; the job is marked failed in _reap
                    pass

            t = threading.Thread(target=body, name=f"job-{job['id']}", daemon=True)
            t.start()
            self.running[job["id"]] = (rid, t)
            return
        cmd = [sys.executable, "-m", "cardforge", "work", rid, "--root", str(self.ws.root),
               "--runs-dir", str(self.ws.runs_dir)]  # fmt: skip
        with open(store.path("worker.log"), "a", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=self.ws.root, start_new_session=True)  # fmt: skip
        store.write(product.WORKER, {"pid": proc.pid, "started": now(), "cmd": cmd, "job": job["id"]})
        self.index.set_pid(job["id"], proc.pid)
        self.running[job["id"]] = (rid, proc)

    def _reap(self) -> None:
        for jid, (rid, handle) in list(self.running.items()):
            if isinstance(handle, threading.Thread):
                if handle.is_alive():
                    continue
                store = RunStore(self.ws.run_dir(rid))
                info = store.read_json(product.WORKER) if store.exists(product.WORKER) else {}
                ok = not info.get("error")
            else:
                if handle.poll() is None:
                    continue
                ok = handle.returncode == 0
            del self.running[jid]
            usage.sync_worker(self.index, self.ws, rid)
            st = refresh(self.index, self.ws, rid) or {}
            rerun = self.index.finish(jid, "done" if ok else "failed", None if ok else "worker exited with an error")
            # A resume while the job ran, or the run still has work and was not stopped.
            store = RunStore(self.ws.run_dir(rid))
            more = st.get("state") == "rendering" and not store.exists(RUN_STOP) and ok
            if rerun or more:
                self.index.enqueue(rid)

    def _retention(self) -> None:
        if self.s.retention_every_h <= 0 or time.monotonic() < self._next_retention:
            return
        self._next_retention = time.monotonic() + self.s.retention_every_h * 3600
        from server.retention import sweep

        out = sweep(self.index, self.ws, self.s.retention_days)
        if out["archived"]:
            print(f"retention: archived {out['archived']}", file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- loop

    def run_forever(self) -> None:
        self.recover()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # keep dispatching; the reason goes to the log
                print(f"dispatcher: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._stop.wait(self.s.worker_poll_s)
        self._drain()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="cardforge-dispatcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _drain(self, grace_s: float = 300) -> None:
        """Wait for running jobs (a paid call in flight must settle its ledger entry)."""
        end = time.monotonic() + grace_s
        while self.running and time.monotonic() < end:
            self._reap()
            time.sleep(0.2)

    def wait_idle(self, timeout: float = 60) -> bool:
        """Tests: until nothing is queued or running."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.tick()
            if not self.running and not self.index.jobs("queued"):
                return True
            time.sleep(0.05)
        return False


def main() -> None:
    from server.core_hooks import install

    s = Settings.from_env()
    index = Index(s.db_path)
    install(index)
    ws = Workspace.from_env(s.home)
    ws = Workspace(root=ws.root, runs_dir=s.runs_dir, refs_dir=ws.refs_dir)
    d = Dispatcher(s, index, ws)
    signal.signal(signal.SIGTERM, lambda *_: d._stop.set())
    print(f"cardforge worker {d.id}: concurrency {s.worker_concurrency}, runs {Path(s.runs_dir)}", flush=True)
    d.run_forever()


if __name__ == "__main__":
    main()
