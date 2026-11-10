"""Retries, the API semaphore and the global GPU lock (TA §7.3, §15.1)."""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_API_CONCURRENCY = 4
MAX_RETRIES = 4
# Codex and local backends run strictly one at a time in the whole process.
GPU_LOCK = threading.Lock()


class TransientError(RuntimeError):
    """429, 5xx or a timeout: retried with exponential backoff."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        timeout: bool = False,
        retry_after_s: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.timeout = timeout
        self.retry_after_s = retry_after_s

    @property
    def answered(self) -> bool:
        """The provider answered with an HTTP error: no image was generated, nothing billed
        beyond a reported cost (the project ledger settles that). A timeout or a transport
        error has an unknown outcome and keeps its estimate."""
        return self.status is not None and not self.timeout


RATE_LIMIT_FACTOR = 5  # a 429 backs off 5× longer than a 5xx (debt T2)
MAX_DELAY_S = 60.0


def retry_delay(exc: TransientError, attempt: int, base_delay_s: float, rng=random.random) -> float:
    """Exponential backoff with jitter; a 429 waits longer and honours `Retry-After`."""
    factor = RATE_LIMIT_FACTOR if exc.status == 429 else 1
    delay = min(MAX_DELAY_S, base_delay_s * factor * 2**attempt)
    if exc.retry_after_s is not None:
        delay = max(delay, min(MAX_DELAY_S, exc.retry_after_s))
    # Jitter spreads parallel runs that were throttled together.
    return delay * (1 + 0.25 * rng()) if exc.status == 429 else delay


def with_retries[T](
    call: Callable[[], T],
    *,
    retries: int = MAX_RETRIES,
    base_delay_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, TransientError], None] | None = None,
) -> T:
    for attempt in range(retries + 1):
        try:
            return call()
        except TransientError as exc:
            if attempt == retries:
                raise
            if on_retry:
                on_retry(attempt + 1, exc)
            sleep(retry_delay(exc, attempt, base_delay_s))
    raise AssertionError("unreachable")


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class SharedSlots:
    """N request slots for one provider shared by every process on the machine (debt T2: three
    parallel runs × 4 threads hit OpenRouter's 429). Each slot is a file lock under `folder`;
    the OS frees a lock when its process dies, so a crash never leaves a slot taken."""

    def __init__(self, folder: Path, slots: int, *, poll_s: float = 0.25, sleep=time.sleep):
        self.folder = Path(folder)
        self.slots = max(1, slots)
        self.poll_s = poll_s
        self.sleep = sleep

    @contextmanager
    def slot(self) -> Iterator[int]:
        self.folder.mkdir(parents=True, exist_ok=True)
        while True:
            for i in range(self.slots):
                fh = open(self.folder / f"slot-{i}.lock", "a+")  # noqa: SIM115
                if _try_lock(fh):
                    try:
                        yield i
                    finally:
                        _unlock(fh)
                        fh.close()
                    return
                fh.close()
            self.sleep(self.poll_s)


class Limiter:
    """API backends share a semaphore (and, when given, the machine-wide provider slots);
    local backends take the global GPU lock."""

    def __init__(
        self,
        *,
        local: bool,
        concurrency: int = DEFAULT_API_CONCURRENCY,
        shared: SharedSlots | None = None,
    ):
        self.local = local
        self.concurrency = 1 if local else max(1, concurrency)
        self.shared = None if local else shared
        self._semaphore = threading.BoundedSemaphore(self.concurrency)

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._semaphore:
            if self.local:
                with GPU_LOCK:
                    yield
            elif self.shared is not None:
                with self.shared.slot():
                    yield
            else:
                yield


def stop_requested(runs_dir: Path) -> bool:
    """Kill switch (TA §12.3): `runs/STOP` or `CARDFORGE_STOP=1` stops after the current call."""
    return os.environ.get("CARDFORGE_STOP") == "1" or (runs_dir / "STOP").exists()
