"""X-Aleph-Key header auth dependency for write endpoints."""

from __future__ import annotations

import logging
import os

from fastapi import Header, HTTPException, status

log = logging.getLogger("aleph")

_WARNED_MISSING = False


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
