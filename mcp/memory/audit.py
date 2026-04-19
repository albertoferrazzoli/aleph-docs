"""Lightweight audit-log writer for the memory system.

Design: best-effort writes that NEVER block or fail the parent call. If the
audit INSERT raises, log a warning and continue — the primary op has already
succeeded (or will succeed) and we don't want to break memory writes because
of an audit hiccup.

Ops vocabulary:
    insert     — first time a memory appears (remember, bootstrap doc_chunks)
    update     — content/embedding updated (doc_chunk re-embed on hash change)
    delete     — forget()
    reinforce  — stability / access_count bumped (search hit, interaction dedup)
    access     — read-only fetch (currently NOT recorded by default, too noisy)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from . import db

log = logging.getLogger("memory")

_TRUNC = 1000


def _audit_reinforce_enabled() -> bool:
    """Read env at call time so tests can toggle via monkeypatch."""
    return os.getenv("AUDIT_REINFORCE", "false").lower() == "true"


async def record(
    op: str,
    *,
    subject_id: Optional[str] = None,
    actor: str = "unknown",
    kind: Optional[str] = None,
    content: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Insert an audit row. Never raises; logs on failure."""
    if op == "reinforce" and not _audit_reinforce_enabled():
        return
    if not db.is_enabled():
        return
    try:
        snippet = (content or "")[:_TRUNC]
        meta = metadata or {}
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO memory_audit (op, subject_id, actor, kind, content, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (op, subject_id, actor, kind, snippet, json.dumps(meta)),
                )
                await conn.commit()
    except Exception as e:
        log.warning(
            "[memory] audit record failed: %s (op=%s subject=%s)",
            e, op, subject_id,
        )
