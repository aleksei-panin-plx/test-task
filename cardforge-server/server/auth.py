# ruff: noqa: E501  (CLI help)
"""Personal keys with scopes, expiry and revocation (portal plan §4; no SSO, user 2026-11-09).

A key `cf_<id>_<secret>` is shown once; the index keeps only its sha256. MCP clients send it
as `Authorization: Bearer cf_…`; the browser exchanges it once for a short HttpOnly session.

Scopes: `read` (status, plans, sheets), `run` (start/resume/stop, agent answers), `decide`
(G1/G2 decisions, regenerate with an edit, G3 choice), `export` (delivery), `admin` (costs,
global stop). Every tool checks the scope and that the run belongs to the key's owner.

    python -m server.auth issue --owner anna --scopes read,run,decide,export [--days 90] [--label laptop]
    python -m server.auth list
    python -m server.auth revoke <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from server.store_index import Index, now

SCOPES = ("read", "run", "decide", "export", "admin")
DEFAULT_SCOPES = ("read", "run", "decide", "export")


@dataclass(frozen=True)
class Principal:
    owner: str
    scopes: frozenset[str]
    credential_id: str
    channel: str  # mcp | web

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def issue(index: Index, owner: str, scopes: list[str] | tuple[str, ...] = DEFAULT_SCOPES,
          *, days: float | None = 90, label: str = "") -> tuple[str, str]:  # fmt: skip
    """(credential id, key). The key is returned once and never stored."""
    owner = owner.strip()
    if not owner or any(c in owner for c in "/\\\0"):
        raise ValueError("owner: a plain name or e-mail")
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"unknown scopes {bad}; known: {', '.join(SCOPES)}")
    cid = secrets.token_hex(4)
    key = f"cf_{cid}_{secrets.token_urlsafe(24)}"
    expires = (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds") if days else None
    with index.tx() as db:
        db.execute(
            "INSERT INTO credentials(id, digest, owner, scopes, label, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (cid, _digest(key), owner, ",".join(dict.fromkeys(scopes)), label, now(), expires),
        )
    return cid, key


def revoke(index: Index, cid: str) -> bool:
    with index.tx() as db:
        cur = db.execute("UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now(), cid))
        db.execute("DELETE FROM sessions WHERE credential_id = ?", (cid,))
        return cur.rowcount == 1


def _valid(row) -> bool:
    if row is None or row["revoked_at"]:
        return False
    return not (row["expires_at"] and row["expires_at"] < now())


def verify(index: Index, key: str | None, channel: str = "mcp") -> Principal | None:
    if not key or not key.startswith("cf_"):
        return None
    rows = index.q("SELECT * FROM credentials WHERE digest = ?", (_digest(key),))
    row = rows[0] if rows else None
    if not _valid(row) or not secrets.compare_digest(row["digest"], _digest(key)):
        return None
    with index.tx() as db:
        db.execute("UPDATE credentials SET last_used_at = ? WHERE id = ?", (now(), row["id"]))
    return Principal(row["owner"], frozenset(row["scopes"].split(",")), row["id"], channel)


def bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


# ---------------------------------------------------------------- browser sessions


def open_session(index: Index, p: Principal, hours: float) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")
    with index.tx() as db:
        db.execute("INSERT INTO sessions(digest, credential_id, owner, scopes, expires_at) VALUES (?,?,?,?,?)",
                   (_digest(token), p.credential_id, p.owner, ",".join(sorted(p.scopes)), expires))  # fmt: skip
    return token


def session(index: Index, token: str | None) -> Principal | None:
    if not token:
        return None
    rows = index.q("SELECT s.*, c.revoked_at, c.expires_at AS cred_expires FROM sessions s "
                   "JOIN credentials c ON c.id = s.credential_id WHERE s.digest = ?", (_digest(token),))  # fmt: skip
    if not rows:
        return None
    r = rows[0]
    if r["expires_at"] < now() or r["revoked_at"] or (r["cred_expires"] and r["cred_expires"] < now()):
        return None
    return Principal(r["owner"], frozenset(r["scopes"].split(",")), r["credential_id"], "web")


def close_session(index: Index, token: str | None) -> None:
    if token:
        with index.tx() as db:
            db.execute("DELETE FROM sessions WHERE digest = ?", (_digest(token),))


def main() -> None:
    from server.settings import Settings

    ap = argparse.ArgumentParser(description="CardForge server keys")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("issue")
    i.add_argument("--owner", required=True)
    i.add_argument("--scopes", default=",".join(DEFAULT_SCOPES))
    i.add_argument("--days", type=float, default=90)
    i.add_argument("--label", default="")
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("id")
    a = ap.parse_args()
    index = Index(Settings.from_env().db_path)
    if a.cmd == "issue":
        cid, key = issue(index, a.owner, a.scopes.split(","), days=a.days or None, label=a.label)
        print(json.dumps({"id": cid, "owner": a.owner, "key": key,
                          "note": "ключ показан один раз; храните его как пароль"}, ensure_ascii=False))  # fmt: skip
    elif a.cmd == "list":
        rows = index.q("SELECT id, owner, scopes, label, created_at, expires_at, revoked_at, last_used_at FROM credentials ORDER BY created_at")
        json.dump([dict(x) for x in rows], sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        print("revoked" if revoke(index, a.id) else "no such active key")


if __name__ == "__main__":
    main()
