"""Session token store for Aleph.

Opaque random tokens persisted in SQLite. Validates username/password
against an htpasswd file (bcrypt-hashed). The resulting session is
carried by the client either as an HttpOnly cookie (browser) or as a
`Authorization: Bearer <token>` header (curl, scripts). A query-param
fallback (`?token=`) exists solely so EventSource — which cannot set
custom headers — can authenticate without forcing browser Basic-Auth
prompts.

Design notes
------------
* Tokens are 32 bytes of `secrets.token_urlsafe` → 43 chars, opaque.
* Storage is SQLite (stdlib, no extra dep) in a file whose path is
  controlled by `ALEPH_SESSIONS_DB`. Default is `./data/sessions.db`
  relative to the backend CWD, matching the pattern used by the
  multi-tenant FIC MCP.
* TTL is configurable via `ALEPH_SESSION_TTL_HOURS` (default 24h).
  Sessions are sliding: each successful validation bumps
  `last_seen_at` and extends `expires_at` to `now + TTL`.
* htpasswd parsing understands bcrypt (`$2y$`, `$2a$`, `$2b$`) which is
  the only hash Apache's `htpasswd -B` produces. Other legacy hashes
  (MD5, SHA1, crypt) are explicitly rejected — callers should rehash
  with bcrypt.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

import bcrypt

log = logging.getLogger("aleph.sessions")

_DEFAULT_TTL_HOURS = 24
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class Session:
    token: str
    username: str
    created_at: int
    expires_at: int


class SessionStore:
    """Thread-safe SQLite-backed session store.

    Not async — SQLite calls are fast (<1ms) and the store is called
    once per request. Wrapping with a lock keeps the contract simple
    and avoids the `check_same_thread` landmine.
    """

    def __init__(self, db_path: Path, ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
        self._path = db_path
        self._ttl_seconds = ttl_hours * 3600
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token         TEXT PRIMARY KEY,
                    username      TEXT NOT NULL,
                    created_at    INTEGER NOT NULL,
                    expires_at    INTEGER NOT NULL,
                    last_seen_at  INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_expires "
                "ON sessions(expires_at)"
            )

    # -- public API ---------------------------------------------------

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def create(self, username: str) -> Session:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = int(time.time())
        expires = now + self._ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(token, username, created_at, "
                "expires_at, last_seen_at) VALUES (?,?,?,?,?)",
                (token, username, now, expires, now),
            )
        return Session(token=token, username=username,
                       created_at=now, expires_at=expires)

    def validate(self, token: str, *, sliding: bool = True) -> Optional[Session]:
        if not token:
            return None
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT token, username, created_at, expires_at "
                "FROM sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            _tok, username, created_at, expires_at = row
            if expires_at <= now:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                return None
            if sliding:
                new_expires = now + self._ttl_seconds
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, expires_at = ? "
                    "WHERE token = ?",
                    (now, new_expires, token),
                )
                expires_at = new_expires
        return Session(token=token, username=username,
                       created_at=created_at, expires_at=expires_at)

    def revoke(self, token: str) -> bool:
        if not token:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return cur.rowcount > 0

    def revoke_all_for(self, username: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE username = ?", (username,)
            )
            return cur.rowcount or 0

    def cleanup_expired(self) -> int:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            )
            return cur.rowcount or 0

    def list_active(self) -> list[Session]:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT token, username, created_at, expires_at "
                "FROM sessions WHERE expires_at > ? "
                "ORDER BY created_at DESC",
                (now,),
            ).fetchall()
        return [
            Session(token=r[0], username=r[1],
                    created_at=r[2], expires_at=r[3])
            for r in rows
        ]


# ---------------------------------------------------------------------
# htpasswd
# ---------------------------------------------------------------------

class HtpasswdFile:
    """Minimal htpasswd parser that only accepts bcrypt hashes."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        users: dict[str, str] = {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("[aleph.sessions] htpasswd read failed: %s", e)
            return {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            user, _, hashv = line.partition(":")
            user = user.strip()
            hashv = hashv.strip()
            if user and hashv:
                users[user] = hashv
        return users

    def verify(self, username: str, password: str) -> bool:
        if not username or not password:
            return False
        users = self._load()
        hashv = users.get(username)
        if not hashv:
            return False
        # Only bcrypt is accepted. `$2y$` is Apache's preferred prefix;
        # bcrypt.checkpw doesn't care about $2y vs $2a vs $2b — they
        # are equivalent at the algorithm level, but some libraries
        # reject $2y. Normalize to $2b for verification.
        if not hashv.startswith(("$2a$", "$2b$", "$2y$")):
            log.warning(
                "[aleph.sessions] user %r uses non-bcrypt hash — "
                "rehash with `htpasswd -B`",
                username,
            )
            return False
        normalised = hashv
        if normalised.startswith("$2y$"):
            normalised = "$2b$" + normalised[4:]
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"),
                normalised.encode("utf-8"),
            )
        except ValueError:
            return False


# ---------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------

_store: Optional[SessionStore] = None
_htpasswd: Optional[HtpasswdFile] = None


def _ttl_hours_from_env() -> int:
    raw = os.getenv("ALEPH_SESSION_TTL_HOURS", "").strip()
    if not raw:
        return _DEFAULT_TTL_HOURS
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_TTL_HOURS
    except ValueError:
        return _DEFAULT_TTL_HOURS


def get_store() -> SessionStore:
    global _store
    if _store is None:
        path = Path(os.getenv("ALEPH_SESSIONS_DB", "./data/sessions.db"))
        _store = SessionStore(path, ttl_hours=_ttl_hours_from_env())
    return _store


def get_htpasswd() -> HtpasswdFile:
    global _htpasswd
    if _htpasswd is None:
        path = Path(os.getenv("ALEPH_HTPASSWD_FILE", "./data/htpasswd"))
        _htpasswd = HtpasswdFile(path)
    return _htpasswd


def reset_for_tests() -> None:
    """Drop module-level singletons — test hook only."""
    global _store, _htpasswd
    _store = None
    _htpasswd = None
