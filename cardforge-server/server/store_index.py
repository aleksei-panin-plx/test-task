# ruff: noqa: E501  (SQL)
"""SQLite index of the server (portal plan §3, PostgreSQL later): runs → owner, state, cost;
the job queue (one active job per run); personal keys; browser sessions; a mirror of the
human decisions for audit. The run folders under `runs/<run_id>/` stay the source of truth in
the core format, so a run started here opens with the core CLI (`status`, `export`) unchanged.

    python -m server.store_index runs|costs|jobs|usage|limits
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, theme TEXT, client TEXT, state TEXT,
  awaiting TEXT, cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'advance',
  status TEXT NOT NULL, claimed_by TEXT, claimed_at TEXT, pid INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  rerun INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(run_id) WHERE status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, scopes TEXT NOT NULL,
  label TEXT, created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  digest TEXT PRIMARY KEY, credential_id TEXT NOT NULL, owner TEXT NOT NULL, scopes TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, owner TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('web', 'mcp', 'worker')), operation TEXT NOT NULL,
  run_id TEXT, cost_usd REAL NOT NULL DEFAULT 0, model TEXT, provider TEXT, ok INTEGER NOT NULL DEFAULT 1,
  source_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS usage_owner_ts ON usage_events(owner, ts);
CREATE TABLE IF NOT EXISTS user_limits (
  owner TEXT PRIMARY KEY, month_limit_usd REAL NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, gate TEXT NOT NULL, owner TEXT NOT NULL,
  channel TEXT NOT NULL, tool TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Index:
    """Thread-safe (one connection per thread, WAL); several processes may share the file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._db().executescript(SCHEMA)  # executescript commits by itself

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            self._local.db = db
        return db

    @contextmanager
    def tx(self):
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._db().execute(sql, args).fetchall()

    # ------------------------------------------------------------ runs

    def add_run(self, run_id: str, owner: str, theme: str, client: str, state: str) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO runs(run_id, owner, theme, client, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, owner, theme, client, state, now(), now()),
            )

    def owner(self, run_id: str) -> str | None:
        rows = self.q("SELECT owner FROM runs WHERE run_id = ?", (run_id,))
        return rows[0]["owner"] if rows else None

    def update_run(self, run_id: str, *, state: str | None, awaiting: str | None, cost_usd: float) -> None:
        with self.tx() as db:
            db.execute("UPDATE runs SET state = ?, awaiting = ?, cost_usd = ?, updated_at = ? WHERE run_id = ?",
                       (state, awaiting, cost_usd, now(), run_id))  # fmt: skip

    def runs(self, owner: str | None = None) -> list[dict]:
        sql = "SELECT * FROM runs" + (" WHERE owner = ?" if owner else "") + " ORDER BY created_at DESC, run_id DESC"
        return [dict(r) for r in self.q(sql, (owner,) if owner else ())]

    def costs_by_owner(self, month: str | None = None) -> dict[str, dict]:
        """Per-owner totals of the runs' ledgers (costs.json). The monthly limit is `server.budget_users`."""
        rows = self.q(
            "SELECT owner, COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost FROM runs"
            + (" WHERE substr(created_at, 1, 7) = ?" if month else "") + " GROUP BY owner ORDER BY owner",
            (month,) if month else (),
        )  # fmt: skip
        return {r["owner"]: {"runs": r["runs"], "cost_usd": round(r["cost"], 6)} for r in rows}

    # ------------------------------------------------------------ queue

    def enqueue(self, run_id: str) -> str:
        """Idempotent: one queued/running job per run. A request while the job runs marks it
        `rerun`, so the dispatcher queues the run again when the job ends (no lost resume)."""
        with self.tx() as db:
            active = db.execute("SELECT id, status FROM jobs WHERE run_id = ? AND status IN ('queued','running')",
                                (run_id,)).fetchone()  # fmt: skip
            if active is None:
                db.execute("INSERT INTO jobs(run_id, status, created_at) VALUES (?, 'queued', ?)", (run_id, now()))
                return "queued"
            if active["status"] == "running":
                db.execute("UPDATE jobs SET rerun = 1 WHERE id = ?", (active["id"],))
            return active["status"]

    def claim(self, worker_id: str) -> dict | None:
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status = 'running', claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
                       "WHERE id = ?", (worker_id, now(), row["id"]))  # fmt: skip
            return dict(row) | {"status": "running", "claimed_by": worker_id}

    def set_pid(self, job_id: int, pid: int) -> None:
        with self.tx() as db:
            db.execute("UPDATE jobs SET pid = ? WHERE id = ?", (pid, job_id))

    def finish(self, job_id: int, status: str, error: str | None = None) -> bool:
        """Close a job; True if a resume arrived meanwhile (`rerun`)."""
        with self.tx() as db:
            row = db.execute("SELECT rerun FROM jobs WHERE id = ?", (job_id,)).fetchone()
            db.execute("UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                       (status, error, now(), job_id))  # fmt: skip
            return bool(row and row["rerun"])

    def requeue_stale(self, worker_id_prefix: str, alive) -> list[int]:
        """After a dispatcher crash: running jobs whose process is gone go back to the queue
        (the core `advance` is idempotent; rendered cards are not redrawn)."""
        back = []
        with self.tx() as db:
            for row in db.execute("SELECT id, pid, claimed_by FROM jobs WHERE status = 'running'").fetchall():
                if not alive(row["pid"]):
                    db.execute("UPDATE jobs SET status = 'queued', pid = NULL, claimed_by = NULL WHERE id = ?", (row["id"],))
                    back.append(row["id"])
        return back

    def job(self, run_id: str) -> dict | None:
        rows = self.q("SELECT * FROM jobs WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,))
        return dict(rows[0]) if rows else None

    def queue_position(self, run_id: str) -> int | None:
        rows = self.q("SELECT run_id FROM jobs WHERE status = 'queued' ORDER BY id")
        ids = [r["run_id"] for r in rows]
        return ids.index(run_id) + 1 if run_id in ids else None

    def jobs(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM jobs" + (" WHERE status = ?" if status else "") + " ORDER BY id"
        return [dict(r) for r in self.q(sql, (status,) if status else ())]

    # ------------------------------------------------------------ usage (one source, server.usage)

    def record_usage(self, *, owner: str, channel: str, operation: str, run_id: str | None = None,
                     cost_usd: float = 0.0, model: str | None = None, provider: str | None = None,
                     ok: bool = True, ts: str | None = None, source_key: str | None = None) -> bool:
        """One attributed event; `source_key` makes a worker call idempotent. True if written."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO usage_events(ts, owner, channel, operation, run_id, cost_usd, model, provider, ok, source_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts or now(), owner, channel, operation, run_id, float(cost_usd), model, provider, int(ok), source_key),
            )
            return cur.rowcount == 1

    def usage(self, owner: str | None = None, channel: str | None = None, month: str | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("owner", owner), ("channel", channel)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if month:
            where.append("substr(ts, 1, 7) = ?")
            args.append(month)
        sql = "SELECT * FROM usage_events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        return [dict(r) for r in self.q(sql, tuple(args))]

    def worker_cost(self, owner: str, month: str) -> float:
        rows = self.q("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage_events WHERE owner = ? AND channel = 'worker' "
                      "AND substr(ts, 1, 7) = ?", (owner, month))  # fmt: skip
        return float(rows[0]["c"])

    # ------------------------------------------------------------ per-user monthly limit

    def set_limit(self, owner: str, usd: float, set_by: str) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO user_limits(owner, month_limit_usd, set_by, set_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(owner) DO UPDATE SET month_limit_usd = excluded.month_limit_usd, "
                       "set_by = excluded.set_by, set_at = excluded.set_at", (owner, float(usd), set_by, now()))  # fmt: skip

    def clear_limit(self, owner: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM user_limits WHERE owner = ?", (owner,))

    def limit(self, owner: str) -> float | None:
        rows = self.q("SELECT month_limit_usd FROM user_limits WHERE owner = ?", (owner,))
        return float(rows[0]["month_limit_usd"]) if rows else None

    def limits(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM user_limits ORDER BY owner")]

    def set_state(self, run_id: str, state: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE runs SET state = ?, awaiting = NULL, updated_at = ? WHERE run_id = ?", (state, now(), run_id))

    def state(self, run_id: str) -> str | None:
        rows = self.q("SELECT state FROM runs WHERE run_id = ?", (run_id,))
        return rows[0]["state"] if rows else None

    # ------------------------------------------------------------ decisions mirror

    def record_decision(self, run_id: str, gate: str, owner: str, channel: str, tool: str, payload: dict) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO decisions(run_id, gate, owner, channel, tool, payload, created_at) VALUES (?,?,?,?,?,?,?)",
                       (run_id, gate, owner, channel, tool, json.dumps(payload, ensure_ascii=False), now()))  # fmt: skip

    def decisions(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM decisions WHERE run_id = ? ORDER BY id", (run_id,))]


def main() -> None:
    from server.settings import Settings

    what = sys.argv[1] if len(sys.argv) > 1 else "runs"
    idx = Index(Settings.from_env().db_path)
    data = {"runs": idx.runs, "costs": idx.costs_by_owner, "jobs": idx.jobs, "usage": idx.usage,
            "limits": idx.limits}[what]()
    json.dump(data, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
