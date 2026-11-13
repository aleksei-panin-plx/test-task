"""Run folder with atomic writes and an append-only journal (TA §7.2-7.4), CardForge Light.

Every file write is atomic (temp file + rename); `events.jsonl` is append-only and fsynced line
by line. The run state lives in `state.json` (Light has no render manifest).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump(data: Any) -> str:
    if isinstance(data, BaseModel):
        return data.model_dump_json(indent=2) + "\n"
    if isinstance(data, list) and data and isinstance(data[0], BaseModel):
        data = [d.model_dump(mode="json") for d in data]
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.run_id = self.root.name
        self.lock = threading.RLock()

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write(self, rel: str, data: Any) -> Path:
        target = self.path(rel)
        write_atomic(target, data if isinstance(data, str) else dump(data))
        return target

    def read_json(self, rel: str) -> Any:
        return json.loads(self.path(rel).read_text(encoding="utf-8"))

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def events(self) -> list[dict]:
        log = self.path("events.jsonl")
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def emit(self, type: str, payload: dict | None = None, **ids: str | None) -> dict:
        event = {
            "event_id": uuid.uuid4().hex,
            "ts": now(),
            "run_id": self.run_id,
            "type": type,
            **{k: v for k, v in ids.items() if v},
            "payload": payload or {},
        }
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path("events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return event
