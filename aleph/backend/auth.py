"""Auth dependencies for Aleph FastAPI routes.

Two orthogonal mechanisms:

* **Session auth** (`require_session`) — gates the human-facing API:
  graph, search, node detail, media, workspaces. Tokens are opaque
  strings produced by `POST /auth/login`. The token travels in *three*
  forms so we cover browser, script, and EventSource clients without
  ever triggering the browser's native Basic-Auth popup:

    1. Cookie `aleph_session` (HttpOnly, Secure, SameSite=Strict) —
       the default for web sessions. Automatically sent by `fetch`
       and `EventSource` with `credentials: 'include' / withCredentials`.
    2. `Authorization: Bearer <token>` — for curl / scripts.
    3. `?token=<token>` query parameter — narrow fallback for clients
       that cannot set headers (legacy EventSource polyfills).

* **Write-key auth** (`require_api_key`) — unchanged. Gates destructive
  endpoints (`/remember`, `/forget`). Sits on TOP of `require_session`.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Header, HTTPException, Query, Request, status

from .sessions import Session, get_store

log = logging.getLogger("aleph")

_WARNED_MISSING = False
SESSION_COOKIE_NAME = "aleph_session"


# ---------------------------------------------------------------------
# Write-key (legacy, unchanged)
# ---------------------------------------------------------------------

def _expected_key() -> str:
    return os.getenv("ALEPH_API_KEY", "").strip()


async def require_api_key(x_aleph_key: str | None = Header(default=None)) -> None:
    global _WARNED_MISSING
    expected = _expected_key()
    if not expected:
        if not _WARNED_MISSING:
            log.warning(
                "[aleph] ALEPH_API_KEY not set; write endpoints will reject all requests"
            )
            _WARNED_MISSING = True
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="write endpoints disabled: ALEPH_API_KEY not configured",
        )
    if not x_aleph_key or x_aleph_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Aleph-Key",
        )


# ---------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------

def _extract_token(
    request: Request,
    authorization: Optional[str],
    query_token: Optional[str],
) -> Optional[str]:
    """Pull a session token out of the request, preferring cookies.

    Order: cookie > Bearer header > query param. The query param is
    explicitly last-resort to discourage logging leaks (access logs
    routinely capture query strings).
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        return cookie
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
    if query_token:
        return query_token.strip() or None
    return None


def _auth_disabled() -> bool:
    """Escape hatch for dev: `ALEPH_AUTH_DISABLED=1` skips all checks.

    Intentionally NOT advertised in .env.example; it exists so a fresh
    clone can boot before the htpasswd file is created. Production
    deploys MUST leave this unset.
    """
    return os.getenv("ALEPH_AUTH_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def require_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> Session:
    """Validate the caller has an active session. Returns the Session."""
    if _auth_disabled():
        return Session(
            token="-", username="anonymous",
            created_at=0, expires_at=2**31 - 1,
        )
    raw = _extract_token(request, authorization, token)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    sess = get_store().validate(raw)
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return sess


async def optional_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> Optional[Session]:
    """Like `require_session` but returns None instead of raising."""
    if _auth_disabled():
        return Session(
            token="-", username="anonymous",
            created_at=0, expires_at=2**31 - 1,
        )
    raw = _extract_token(request, authorization, token)
    if not raw:
        return None
    return get_store().validate(raw)
