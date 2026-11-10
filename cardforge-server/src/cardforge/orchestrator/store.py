"""Run folder with atomic writes, an append-only journal and a checked manifest (TA §7.2-7.4).

Stage 3 used request, plan, policy screen, manifest and report; stage 4 adds the card
attempt tree, costs and gate files. Every file write is atomic (temp file + rename); the
journal is append-only and fsynced line by line.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from cardforge.contracts.run import Event, RunManifest

M = TypeVar("M", bound=BaseModel)


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


def write_atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
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


class ManifestError(ValueError):
    pass


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        self.run_id = root.name
        # Render workers finish in other threads; journal and manifest writes stay serial.
        self.lock = threading.RLock()

    @classmethod
    def create(cls, root: Path, **manifest: Any) -> RunStore:
        if (root / "manifest.json").exists():
            raise FileExistsError(f"{root} already holds a run")
        store = cls(root)
        store.save_manifest(RunManifest(run_id=store.run_id, created_at=now(), **manifest))
        return store

    def path(self, rel: str) -> Path:
        return self.root / rel

    def write(self, rel: str, data: Any) -> Path:
        target = self.path(rel)
        write_atomic(target, data if isinstance(data, str) else dump(data))
        return target

    def read_json(self, rel: str) -> Any:
        return json.loads(self.path(rel).read_text(encoding="utf-8"))

    def read_model(self, rel: str, model: type[M]) -> M:
        return model.model_validate_json(self.path(rel).read_text(encoding="utf-8"))

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def events(self) -> list[Event]:
        log = self.path("events.jsonl")
        if not log.exists():
            return []
        return [Event.model_validate_json(x) for x in log.read_text().splitlines() if x.strip()]

    def emit(self, type: str, payload: dict | None = None, **ids: str | None) -> Event:
        event = Event(
            event_id=uuid.uuid4().hex,
            ts=now(),
            run_id=self.run_id,
            type=type,
            payload=payload or {},
            **ids,
        )
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path("events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return event

    def manifest(self) -> RunManifest:
        manifest = RunManifest.model_validate_json(self.path("manifest.json").read_text())
        self.check_manifest(manifest)
        return manifest

    def check_manifest(self, manifest: RunManifest) -> None:
        """`ip_check: confirmed` must point at a human G3 decision for that variant (TA §9.8)."""
        if all(c.status != "confirmed" for c in manifest.ip_check.values()):
            return
        decisions = {e.event_id: e for e in self.events() if e.type == "human_decision"}
        for variant, check in manifest.ip_check.items():
            if check.status != "confirmed":
                continue
            event = decisions.get(check.event_id or "")
            if (
                event is None
                or event.variant_id != variant
                or event.payload.get("subject") != "ip_check"
                or event.payload.get("decision") != "confirmed"
                or event.payload.get("decided_by") != "human"
            ):
                raise ManifestError(
                    f"{variant}: ip_check confirmed without a matching human_decision event"
                )

    def save_manifest(self, manifest: RunManifest) -> None:
        with self.lock:
            self.check_manifest(manifest)
            self.write("manifest.json", manifest)

    def update_manifest(self, change: Callable[[RunManifest], None]) -> RunManifest:
        """Read, change and write the manifest under the store lock."""
        with self.lock:
            manifest = self.manifest()
            change(manifest)
            self.save_manifest(manifest)
            return manifest
