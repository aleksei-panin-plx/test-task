# ruff: noqa: E501
"""Run retention (user 2026-11-10: runs are kept **30 days after delivery**, then archived).

A run is archived when its delivery (`delivery/current/delivery.json`) exists, nothing happened in
the run for `RUN_RETENTION_DAYS` (default 30; the later of the delivery file and the run journal
`events.jsonl`, so a regenerate after delivery restarts the clock) and it has no queued or running
job. Archive: `runs/_archive/<run>.tar.gz` (the whole run folder, verified by reading it back), then
the folder is removed and the index marks the run `archived`; tools answer «в архиве». Runs without a
delivery are never archived by this task. Nothing leaves the machine; restoring is unpacking the
archive into `runs/` and `python -m server.retention restore <run>`.

    python -m server.retention [--dry-run]       # also run by the dispatcher every RETENTION_EVERY_H hours
    python -m server.retention restore <run>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import time
from pathlib import Path

from cardforge.orchestrator.workspace import Workspace
from server.settings import Settings
from server.store_index import Index

DELIVERY = "delivery/current/delivery.json"


def archive_dir(ws: Workspace) -> Path:
    return ws.runs_dir / "_archive"


def due(index: Index, ws: Workspace, days: float, *, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    out = []
    for r in index.runs():
        rid = r["run_id"]
        folder = ws.run_dir(rid)
        if r["state"] == "archived" or not (folder / DELIVERY).exists():
            continue
        job = index.job(rid)
        if job and job["status"] in ("queued", "running"):
            continue
        last = max((folder / DELIVERY).stat().st_mtime,
                   (folder / "events.jsonl").stat().st_mtime if (folder / "events.jsonl").exists() else 0)  # fmt: skip
        if now - last >= days * 86400:
            out.append(rid)
    return out


def archive(index: Index, ws: Workspace, run_id: str) -> Path:
    folder = ws.run_dir(run_id)
    target = archive_dir(ws) / f"{run_id}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(folder, arcname=run_id)
    with tarfile.open(tmp, "r:gz") as tar:  # verify before deleting anything
        names = tar.getnames()
    files = sum(1 for p in folder.rglob("*") if p.is_file())
    if sum(1 for n in names if n != run_id) < files:
        tmp.unlink()
        raise RuntimeError(f"{run_id}: archive incomplete, folder kept")
    tmp.replace(target)
    shutil.rmtree(folder)
    index.set_state(run_id, "archived")
    return target


def restore(index: Index, ws: Workspace, run_id: str) -> Path:
    src = archive_dir(ws) / f"{run_id}.tar.gz"
    with tarfile.open(src, "r:gz") as tar:
        tar.extractall(ws.runs_dir, filter="data")
    from server.worker import refresh

    refresh(index, ws, run_id)
    return ws.run_dir(run_id)


def sweep(index: Index, ws: Workspace, days: float, *, dry_run: bool = False) -> dict:
    ids = due(index, ws, days)
    done = [] if dry_run else [archive(index, ws, rid).name for rid in ids]
    return {"days": days, "due": ids, "archived": done, "dry_run": dry_run}


def main() -> None:
    ap = argparse.ArgumentParser(description="CardForge server: run retention")
    ap.add_argument("cmd", nargs="?", default="sweep", choices=["sweep", "restore"])
    ap.add_argument("run", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    s = Settings.from_env()
    index = Index(s.db_path)
    base = Workspace.from_env(s.home)
    ws = Workspace(root=base.root, runs_dir=s.runs_dir, refs_dir=base.refs_dir)
    out = {"restored": str(restore(index, ws, a.run))} if a.cmd == "restore" else sweep(index, ws, s.retention_days, dry_run=a.dry_run)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
