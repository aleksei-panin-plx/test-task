# ruff: noqa: E501
"""Server settings from the environment (`.env.example` lists them). Keys stay in the
environment; nothing here is logged except the presence of a key."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    home: Path
    runs_dir: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8006
    public_url: str = ""
    worker_concurrency: int = 2
    # embedded: the dispatcher runs inside the app process; external: `python -m server.worker`.
    worker: str = "embedded"
    # process: each job is `python -m cardforge work <run>` (own pid → the paid lock works across
    # jobs); inline: in a thread of the dispatcher (tests, FakeBackend debugging only).
    worker_mode: str = "process"
    worker_poll_s: float = 1.0
    # Offline demo / e2e: start_run uses the fake planner and FakeBackend (no key, no network).
    demo: bool = False
    # Channels allowed to take G3 actions (select_variant, regenerate_cards, export_run).
    g3_channels: tuple[str, ...] = ("mcp", "web")
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    session_hours: float = 12.0
    # User 2026-11-10: monthly OpenRouter limit per user, $50 by default, changed by the admin.
    user_month_limit_usd: float = 50.0
    # User 2026-11-10: runs kept 30 days after delivery, then archived (server.retention).
    retention_days: float = 30.0
    retention_every_h: float = 6.0

    @classmethod
    def from_env(cls) -> Settings:
        home = Path(os.environ.get("CARDFORGE_HOME") or FOLDER).resolve()
        runs = Path(os.environ.get("CARDFORGE_RUNS_DIR") or home / "runs").resolve()
        host = os.environ.get("CARDFORGE_SERVER_HOST", "127.0.0.1")
        port = int(os.environ.get("CARDFORGE_SERVER_PORT", "8006"))
        public = os.environ.get("CARDFORGE_PUBLIC_URL") or f"http://{host}:{port}"
        origins = tuple(o.strip() for o in os.environ.get("CARDFORGE_ALLOWED_ORIGINS", "").split(",") if o.strip())
        channels = tuple(c.strip() for c in os.environ.get("CARDFORGE_G3_CHANNELS", "mcp,web").split(",") if c.strip())
        return cls(
            home=home,
            runs_dir=runs,
            db_path=Path(os.environ.get("CARDFORGE_SERVER_DB") or runs / "_server/index.sqlite3"),
            host=host,
            port=port,
            public_url=public.rstrip("/"),
            worker_concurrency=max(1, int(os.environ.get("WORKER_CONCURRENCY", "2"))),
            worker=os.environ.get("CARDFORGE_SERVER_WORKER", "embedded"),
            worker_mode=os.environ.get("CARDFORGE_WORKER_MODE", "process"),
            worker_poll_s=float(os.environ.get("CARDFORGE_WORKER_POLL_S", "1.0")),
            demo=_bool("CARDFORGE_SERVER_DEMO", False),
            g3_channels=channels,
            allowed_origins=origins or (public.rstrip("/"),),
            session_hours=float(os.environ.get("CARDFORGE_SESSION_HOURS", "12")),
            user_month_limit_usd=float(os.environ.get("USER_MONTH_LIMIT_USD", "50")),
            retention_days=float(os.environ.get("RUN_RETENTION_DAYS", "30")),
            retention_every_h=float(os.environ.get("RETENTION_EVERY_H", "6")),
        )
