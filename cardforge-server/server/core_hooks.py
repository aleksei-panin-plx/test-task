# ruff: noqa: E501
"""The two places where the server plugs into the unchanged core copy (no edit of `src/cardforge/`).

1. `service.create_run` gets `owner` (the key's owner) and `deployment="server"` from the
   request context, so every run manifest says who started it and under which profile
   (TA §13.4 п.4) and the `[deployment.server]` allow-lists of `config/runtime.toml` apply.
2. `product._start_worker` (the local plugin spawns `cardforge work` itself) becomes «put a
   job into the server queue»: the dispatcher (`server.worker`) starts it within
   `WORKER_CONCURRENCY`.

Both are module attributes looked up at call time, so the patch is thread-safe. A core change
that renames them breaks `tests_server/test_core_hooks.py` instead of failing silently. A core
hook for both (so this module is not needed) is proposed to the stage 7 session (docs/acceptance.md).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from cardforge.orchestrator import product, service

OWNER: ContextVar[str | None] = ContextVar("cardforge_owner", default=None)
CLIENT: ContextVar[str] = ContextVar("cardforge_client", default="mcp")
_ORIGINAL = {"create_run": service.create_run, "start_worker": product._start_worker}
_INDEX = None


@contextmanager
def acting_as(owner: str, client: str = "mcp"):
    t1, t2 = OWNER.set(owner), CLIENT.set(client)
    try:
        yield
    finally:
        OWNER.reset(t1)
        CLIENT.reset(t2)


def _create_run(ws, request, **kw):
    owner = OWNER.get()
    if owner is not None:
        kw["owner"] = owner
        kw["deployment"] = "server"
        kw["client"] = CLIENT.get()
    run_id = _ORIGINAL["create_run"](ws, request, **kw)
    if owner is not None:
        # distribution-options §5: a run says which copy made it (core and server versions).
        import cardforge
        from cardforge.orchestrator.store import RunStore
        from server import SERVER_VERSION

        def stamp(m) -> None:
            m.versions["core_version"] = cardforge.__version__
            m.versions["server_version"] = SERVER_VERSION

        RunStore(ws.run_dir(run_id)).update_manifest(stamp)
    return run_id


def _enqueue(ws, run_id: str) -> None:
    if _INDEX is None:
        raise RuntimeError("server queue is not installed")
    _INDEX.enqueue(run_id)


def install(index) -> None:
    global _INDEX
    _INDEX = index
    service.create_run = _create_run
    product._start_worker = _enqueue


def uninstall() -> None:
    global _INDEX
    _INDEX = None
    service.create_run = _ORIGINAL["create_run"]
    product._start_worker = _ORIGINAL["start_worker"]
