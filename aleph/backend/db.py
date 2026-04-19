"""Thin DB wrapper for Aleph.

Re-exports the aleph-docs-mcp MCP memory pool (via sys.path shim) and adds
graph_snapshot CRUD + a LISTEN helper for the SSE stream.

MCP location is resolved in this order:
    1. $MCP_PATH env var (absolute path to a dir containing `memory/`)
    2. /opt/mcp  (production VM layout)
    3. ../../mcp (worktree layout, relative to this file)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aleph")


def _resolve_mcp_path() -> Path:
    env = os.getenv("MCP_PATH", "").strip()
    if env:
        return Path(env).resolve()

    prod = Path("/opt/mcp")
    if prod.is_dir():
        return prod

    # worktree fallback: backend/db.py -> backend -> aleph -> worktree/mcp
    here = Path(__file__).resolve().parent
    return (here.parent.parent / "mcp" / "aleph-docs-mcp").resolve()


_MCP_PATH = _resolve_mcp_path()
if str(_MCP_PATH) not in sys.path:
    sys.path.insert(0, str(_MCP_PATH))

# Re-export memory subsystem.
from memory import db as _mem_db  # noqa: E402
from memory import store as _mem_store  # noqa: E402

MemoryDisabled = _mem_db.MemoryDisabled
is_enabled = _mem_db.is_enabled
init_pool = _mem_db.init_pool
close_pool = _mem_db.close_pool
get_conn = _mem_db.get_conn
health_check = _mem_db.health_check
store = _mem_store


# ---------------------------------------------------------------------------
# graph_snapshot CRUD
# ---------------------------------------------------------------------------

async def get_latest_snapshot() -> Optional[dict]:
    """Return the most-recent graph_snapshot row or None."""
    if not is_enabled():
        return None
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT version, payload, created_at "
                    "FROM graph_snapshot ORDER BY version DESC LIMIT 1"
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                version, payload, created_at = row
                if isinstance(payload, (str, bytes)):
                    payload = json.loads(payload)
                return {
                    "version": int(version),
                    "payload": payload,
                    "created_at": created_at.isoformat() if created_at else None,
                }
    except MemoryDisabled:
        return None


async def get_latest_version() -> Optional[int]:
    if not is_enabled():
        return None
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COALESCE(MAX(version), 0) FROM graph_snapshot")
                row = await cur.fetchone()
                v = int(row[0]) if row else 0
                return v if v > 0 else None
    except MemoryDisabled:
        return None


async def insert_snapshot(payload: dict) -> int:
    """Insert a new snapshot with monotonic version. Keep last 4."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(version), 0) FROM graph_snapshot")
            row = await cur.fetchone()
            next_version = int(row[0]) + 1 if row else 1
            await cur.execute(
                "INSERT INTO graph_snapshot (version, payload) VALUES (%s, %s::jsonb)",
                (next_version, json.dumps(payload)),
            )
            await cur.execute(
                "DELETE FROM graph_snapshot WHERE version < %s",
                (next_version - 3,),
            )
            try:
                await cur.execute(f"NOTIFY graph_rebuilt, '{next_version}'")
            except Exception as e:  # pragma: no cover
                log.debug("[aleph] NOTIFY graph_rebuilt failed: %s", e)
        await conn.commit()
    return next_version


async def count_memories() -> Optional[int]:
    if not is_enabled():
        return None
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM memories")
                row = await cur.fetchone()
                return int(row[0]) if row else 0
    except MemoryDisabled:
        return None


async def get_node(node_id: str) -> Optional[dict]:
    """Full row for /node/{id} — WITHOUT the embedding column."""
    if not is_enabled():
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, kind::text, content, source_path, source_section, "
                "       metadata, created_at, last_access_at, access_count, "
                "       stability "
                "FROM memories WHERE id = %s",
                (node_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            (rid, kind, content, sp, ss, meta, created, last_access,
             ac, stab) = row
            return {
                "id": str(rid),
                "kind": kind,
                "content": content,
                "source_path": sp,
                "source_section": ss,
                "metadata": meta or {},
                "created_at": created.isoformat() if created else None,
                "last_access_at": last_access.isoformat() if last_access else None,
                "access_count": int(ac),
                "stability": float(stab),
            }


async def get_neighbors(node_id: str, k: int = 8, min_w: float = 0.4) -> list[dict]:
    """Top-k cosine neighbors of `node_id`, filtered by w > min_w."""
    if not is_enabled():
        return []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT m.id, m.kind::text, m.content, "
                "       1 - (m.embedding <=> a.embedding) AS w "
                "FROM memories m, "
                "     (SELECT embedding FROM memories WHERE id = %s) a "
                "WHERE m.id <> %s "
                "ORDER BY m.embedding <=> a.embedding "
                "LIMIT %s",
                (node_id, node_id, k),
            )
            rows = await cur.fetchall()
    out = []
    for rid, kind, content, w in rows:
        w_f = float(w) if w is not None else 0.0
        if w_f <= min_w:
            continue
        out.append({
            "id": str(rid),
            "kind": kind,
            "content": content,
            "w": w_f,
        })
    return out


async def fetch_pending_memories(known_ids: set, limit: int = 500) -> list:
    """Return memories NOT in `known_ids` (rows created since the latest
    graph_snapshot was built), each enriched with its top-3 nearest-neighbor
    IDs among `known_ids` so the client can anchor their position.

    Without this, memories inserted between two hourly projection runs are
    invisible after a page reload (snapshot is stale). With this, /graph
    returns them in a `pending` field and the frontend places them at the
    centroid of their anchor neighbors (same logic as the live SSE path).
    """
    if not known_ids:
        return []
    async with _mem_db.get_conn() as conn:
        async with conn.cursor() as cur:
            known_list = list(known_ids)
            await cur.execute(
                "SELECT id, kind::text, content, source_path, source_section, "
                "       metadata, created_at, last_access_at, access_count, "
                "       stability "
                "FROM memories "
                "WHERE NOT (id::text = ANY(%s)) "
                "ORDER BY created_at DESC "
                "LIMIT %s",
                (known_list, limit),
            )
            pending_rows = await cur.fetchall()
            out = []
            for row in pending_rows:
                (rid, kind, content, sp, ss, meta, created, last_access,
                 ac, stab) = row
                # Top-3 neighbors restricted to known (already-positioned) nodes.
                await cur.execute(
                    "SELECT m.id::text "
                    "FROM memories m, "
                    "     (SELECT embedding FROM memories WHERE id = %s) a "
                    "WHERE m.id::text = ANY(%s) AND m.id <> %s "
                    "ORDER BY m.embedding <=> a.embedding "
                    "LIMIT 3",
                    (rid, known_list, rid),
                )
                anchor_ids = [r[0] for r in await cur.fetchall()]
                out.append({
                    "id": str(rid),
                    "kind": kind,
                    "content": content,
                    "source_path": sp,
                    "source_section": ss,
                    "metadata": meta or {},
                    "created_at": created.isoformat() if created else None,
                    "last_access_at": last_access.isoformat() if last_access else None,
                    "access_count": int(ac),
                    "stability": float(stab),
                    "anchor_ids": anchor_ids,
                })
    return out


async def get_node_audit(node_id: str, limit: int = 20) -> list[dict]:
    """Read memory_audit rows for a specific subject_id. Best-effort: if the
    audit table doesn't exist (old MCP deploy) returns empty list."""
    async with _mem_db.get_conn() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    SELECT id, ts, op, actor, kind,
                           LEFT(content, 200) AS content, metadata
                    FROM memory_audit
                    WHERE subject_id = %s
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (node_id, limit),
                )
                rows = await cur.fetchall()
            except Exception:
                return []
    out = []
    for r in rows:
        ts = r[1]
        out.append({
            "id": r[0],
            "ts": ts.isoformat() if ts else None,
            "ts_ms": int(ts.timestamp() * 1000) if ts else None,
            "op": r[2],
            "actor": r[3],
            "kind": r[4],
            "content": r[5],
            "metadata": r[6] or {},
        })
    return out


# ---------------------------------------------------------------------------
# LISTEN helper (dedicated connection, not pooled)
# ---------------------------------------------------------------------------

def raw_dsn() -> str:
    return os.getenv("PG_DSN", "").strip()
