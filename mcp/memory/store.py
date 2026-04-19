"""CRUD + search layer for semantic memory.

All functions are async. Writes degrade to no-op when memory is disabled;
search() raises MemoryDisabled so the tool layer can surface a structured
error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from pgvector.psycopg import Vector

from . import audit, chunker, db, embeddings
from .embedders import get_backend
from .types import MediaChunk

log = logging.getLogger("memory")


# ---------------------------------------------------------------------------
# Stability defaults (PRD §3.3)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "doc_chunk": 30.0,
    "insight": 14.0,
    "interaction": 3.0,
}


def _stability_for(kind: str) -> float:
    env_key = f"STABILITY_{kind.upper()}"
    raw = os.getenv(env_key)
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("[memory] invalid %s=%r, using default", env_key, raw)
    return _DEFAULTS.get(kind, 7.0)


def _vec(e):
    return Vector(e) if not isinstance(e, Vector) else e


# ---------------------------------------------------------------------------
# upsert_doc_chunks
# ---------------------------------------------------------------------------

async def upsert_doc_chunks(
    rel_path: str,
    chunks: list["chunker.Chunk"],
    mtime: int | None = None,
) -> dict:
    """Upsert chunks for one doc page. Skip rows whose hash is unchanged."""
    if not db.is_enabled():
        log.debug("[memory] upsert_doc_chunks skipped: memory disabled")
        return {"inserted": 0, "updated": 0, "skipped": 0, "deleted": 0}

    stability = _stability_for("doc_chunk")
    new_sections = {c.section_anchor for c in chunks}

    inserted = 0
    updated = 0
    skipped = 0
    deleted = 0

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            # 1) Load existing rows for this source_path
            await cur.execute(
                "SELECT source_section, metadata, id FROM memories "
                "WHERE kind='doc_chunk' AND source_path=%s",
                (rel_path,),
            )
            existing_rows = await cur.fetchall()
            existing = {
                row[0]: (row[1] or {}) for row in existing_rows
            }
            existing_ids = {row[0]: row[2] for row in existing_rows}

            # 2) Decide per-chunk what to do
            to_embed: list[chunker.Chunk] = []
            actions: list[str] = []  # parallel: "insert" | "update" | "skip"
            for c in chunks:
                prev = existing.get(c.section_anchor)
                prev_hash = (prev or {}).get("hash") if prev else None
                if prev is None:
                    to_embed.append(c)
                    actions.append("insert")
                elif prev_hash != c.hash:
                    to_embed.append(c)
                    actions.append("update")
                else:
                    actions.append("skip")
                    skipped += 1

            # 3) Embed only the needed ones in a single batch
            vectors: list[list[float]] = []
            if to_embed:
                vectors = await embeddings.embed_batch([c.content for c in to_embed])

            # 4) Apply inserts/updates
            # Collect audit entries to emit AFTER commit so failures never
            # roll back the primary write.
            audit_events: list[dict] = []
            vi = 0
            for c, action in zip(chunks, actions):
                if action == "skip":
                    continue
                emb = vectors[vi]
                vi += 1
                meta = dict(c.metadata or {})
                meta["hash"] = c.hash
                if mtime is not None:
                    meta["mtime"] = mtime
                if action == "insert":
                    await cur.execute(
                        "INSERT INTO memories (kind, content, source_path, source_section, "
                        " metadata, embedding, stability) "
                        "VALUES ('doc_chunk', %s, %s, %s, %s::jsonb, %s, %s) "
                        "RETURNING id",
                        (
                            c.content,
                            rel_path,
                            c.section_anchor,
                            json.dumps(meta),
                            _vec(emb),
                            stability,
                        ),
                    )
                    new_row = await cur.fetchone()
                    inserted += 1
                    audit_events.append({
                        "op": "insert",
                        "subject_id": str(new_row[0]) if new_row else None,
                        "actor": "indexer:bootstrap",
                        "kind": "doc_chunk",
                        "content": c.content,
                        "metadata": {"rel_path": rel_path, "section": c.section_anchor},
                    })
                else:  # update
                    await cur.execute(
                        "UPDATE memories "
                        "SET content=%s, embedding=%s, metadata=%s::jsonb, "
                        "    stability=%s, last_access_at=now() "
                        "WHERE kind='doc_chunk' AND source_path=%s "
                        "  AND source_section=%s",
                        (
                            c.content,
                            _vec(emb),
                            json.dumps(meta),
                            stability,
                            rel_path,
                            c.section_anchor,
                        ),
                    )
                    updated += 1
                    prev_id = existing_ids.get(c.section_anchor)
                    audit_events.append({
                        "op": "update",
                        "subject_id": str(prev_id) if prev_id else None,
                        "actor": "indexer:bootstrap",
                        "kind": "doc_chunk",
                        "content": c.content,
                        "metadata": {
                            "rel_path": rel_path,
                            "section": c.section_anchor,
                            "reason": "hash_change",
                        },
                    })

            # 5) Delete stale rows
            stale = [s for s in existing if s not in new_sections]
            if stale:
                await cur.execute(
                    "DELETE FROM memories "
                    "WHERE kind='doc_chunk' AND source_path=%s "
                    "  AND source_section = ANY(%s)",
                    (rel_path, stale),
                )
                deleted = len(stale)
                for s in stale:
                    prev_id = existing_ids.get(s)
                    audit_events.append({
                        "op": "delete",
                        "subject_id": str(prev_id) if prev_id else None,
                        "actor": "indexer:bootstrap",
                        "kind": "doc_chunk",
                        "content": None,
                        "metadata": {"rel_path": rel_path, "section": s},
                    })

        await conn.commit()

    for ev in audit_events:
        await audit.record(
            ev["op"],
            subject_id=ev["subject_id"],
            actor=ev["actor"],
            kind=ev["kind"],
            content=ev["content"],
            metadata=ev["metadata"],
        )

    log.debug(
        "[memory] upsert_doc_chunks %s: inserted=%d updated=%d skipped=%d deleted=%d",
        rel_path, inserted, updated, skipped, deleted,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "deleted": deleted,
    }


# ---------------------------------------------------------------------------
# upsert_interaction
# ---------------------------------------------------------------------------

async def upsert_interaction(
    query: str,
    tool_name: str,
    top_result: dict | None,
) -> dict:
    """Record a tool query; dedup against nearest interaction (sim>0.9)."""
    if not db.is_enabled():
        log.debug("[memory] upsert_interaction skipped: memory disabled")
        return {"action": "skipped", "id": None, "similarity": None}

    if not query or not query.strip():
        return {"action": "skipped", "id": None, "similarity": None}

    stability = _stability_for("interaction")

    try:
        emb = await embeddings.embed_one(query)
    except Exception as e:
        log.warning("[memory] upsert_interaction embed failed: %s", e)
        return {"action": "skipped", "id": None, "similarity": None}

    meta = {
        "tool": tool_name,
        "top_path": top_result.get("path") if isinstance(top_result, dict) else None,
        "top_id": top_result.get("id") if isinstance(top_result, dict) else None,
    }

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, 1 - (embedding <=> %s) AS sim FROM memories "
                "WHERE kind='interaction' "
                "ORDER BY embedding <=> %s LIMIT 1",
                (_vec(emb), _vec(emb)),
            )
            row = await cur.fetchone()
            if row and row[1] is not None and float(row[1]) > 0.9:
                mid = row[0]
                sim = float(row[1])
                await cur.execute(
                    "UPDATE memories "
                    "SET access_count = access_count + 1, "
                    "    stability = LEAST(stability * 1.7, 365), "
                    "    last_access_at = now() "
                    "WHERE id = %s",
                    (mid,),
                )
                await conn.commit()
                await audit.record(
                    "reinforce",
                    subject_id=str(mid),
                    actor="mcp:auto-record",
                    kind="interaction",
                    content=query,
                    metadata={"tool": tool_name, "similarity": sim},
                )
                return {"action": "reinforced", "id": str(mid), "similarity": sim}

            await cur.execute(
                "INSERT INTO memories (kind, content, metadata, embedding, stability) "
                "VALUES ('interaction', %s, %s::jsonb, %s, %s) "
                "RETURNING id",
                (query, json.dumps(meta), _vec(emb), stability),
            )
            new = await cur.fetchone()
            await conn.commit()
            new_id = str(new[0])
            await audit.record(
                "insert",
                subject_id=new_id,
                actor="mcp:auto-record",
                kind="interaction",
                content=query,
                metadata={"tool": tool_name},
            )
            return {"action": "inserted", "id": new_id, "similarity": None}


# ---------------------------------------------------------------------------
# insert_insight
# ---------------------------------------------------------------------------

async def insert_insight(
    content: str,
    context: str = "",
    source_path: str | None = None,
    tags: list[str] | None = None,
    *,
    actor: str = "mcp:remember",
) -> dict:
    if not db.is_enabled():
        log.debug("[memory] insert_insight skipped: memory disabled")
        return {"action": "skipped", "id": None}

    full = f"{content}\n\n{context}".strip() if context else content
    emb = await embeddings.embed_one(full)
    meta = {"tags": tags or [], "context": context or ""}
    stability = _stability_for("insight")

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memories (kind, content, source_path, metadata, "
                "                      embedding, stability) "
                "VALUES ('insight', %s, %s, %s::jsonb, %s, %s) RETURNING id",
                (content, source_path, json.dumps(meta), _vec(emb), stability),
            )
            row = await cur.fetchone()
            await conn.commit()
            new_id = str(row[0])

    await audit.record(
        "insert",
        subject_id=new_id,
        actor=actor,
        kind="insight",
        content=content,
        metadata={
            "context": context,
            "source_path": source_path,
            "tags": tags or [],
        },
    )
    return {"id": new_id}


# ---------------------------------------------------------------------------
# upsert_media_chunk (multimodal)
# ---------------------------------------------------------------------------


# Which `MediaChunk.kind` values map to which backend modality capability.
_KIND_TO_MODALITY = {
    "image": "image",
    "video_scene": "video",
    "audio_clip": "audio",
    "pdf_page": "pdf",
}


async def upsert_media_chunk(
    chunk: MediaChunk,
    *,
    actor: str = "mcp:remember_media",
    context: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Embed + insert a single media memory row.

    Validates that the active embedder backend supports the modality
    required by `chunk.kind` BEFORE spending any tokens. If not, raises
    RuntimeError with the active backend name — the tool layer surfaces
    this as a structured error so callers know to switch backends.

    Returns {"id": str, "kind": str, "media_type": str}.
    """
    if not db.is_enabled():
        log.debug("[memory] upsert_media_chunk skipped: memory disabled")
        raise db.MemoryDisabled(
            "memory subsystem is disabled or not initialized"
        )

    required = _KIND_TO_MODALITY.get(chunk.kind)
    if required is None:
        raise RuntimeError(
            f"upsert_media_chunk: unknown media kind {chunk.kind!r}"
        )

    # Fail fast if the current backend cannot embed this modality.
    backend = get_backend()
    if required not in backend.modalities:
        raise RuntimeError(
            f"active embedder backend {backend.name!r} does not support "
            f"modality {required!r} (needed for kind={chunk.kind!r}). "
            f"Supported: {sorted(backend.modalities)}. "
            f"Set EMBED_BACKEND to a multimodal backend "
            f"(e.g. 'gemini-2-preview')."
        )

    if chunk.path is None:
        raise RuntimeError(
            f"upsert_media_chunk: chunk.path is None for kind={chunk.kind!r}"
        )

    # Embed via the shared embeddings shim so tests can monkeypatch it.
    # It forwards to the active backend with EMBED_DIM guarding.
    vectors = await embeddings.embed_batch([chunk.path])
    if not vectors:
        raise RuntimeError("upsert_media_chunk: backend returned no vectors")
    emb = vectors[0]

    stability = _stability_for("insight")  # media ≈ insight for now
    meta = dict(chunk.metadata or {})
    if context:
        meta["context"] = context
    if tags:
        meta["tags"] = list(tags)

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memories "
                "  (kind, content, source_path, metadata, embedding, "
                "   stability, media_ref, media_type, preview_b64) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    chunk.kind,
                    chunk.content,
                    chunk.media_ref,
                    json.dumps(meta),
                    _vec(emb),
                    stability,
                    chunk.media_ref,
                    chunk.media_type,
                    chunk.preview_b64,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()
            new_id = str(row[0])

    await audit.record(
        "insert",
        subject_id=new_id,
        actor=actor,
        kind=chunk.kind,
        content=chunk.content,
        metadata={
            "media_ref": chunk.media_ref,
            "media_type": chunk.media_type,
            "context": context,
            "tags": tags or [],
            **{k: v for k, v in (chunk.metadata or {}).items()
               if k in ("sha256", "w", "h", "bytes", "t_start_s", "t_end_s")},
        },
    )
    return {
        "id": new_id,
        "kind": chunk.kind,
        "media_type": chunk.media_type,
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

async def search(
    query: str,
    kind: str | None = None,
    limit: int = 10,
    min_score: float = 0.15,
) -> list[dict]:
    """Vector search with forgetting-curve score and atomic reinforcement."""
    if not db.is_enabled():
        raise db.MemoryDisabled("memory subsystem is disabled or not initialized")

    emb = await embeddings.embed_one(query)
    v = _vec(emb)

    where_clause = ""
    params: list = [v]
    if kind:
        where_clause = "WHERE kind = %s"
        params.append(kind)
    params.append(v)           # ORDER BY
    params.append(limit * 3)   # hits LIMIT
    params.append(min_score)   # scored filter
    params.append(limit)       # ranked LIMIT

    sql = f"""
    WITH hits AS (
        SELECT id, kind, content, source_path, source_section, metadata,
               1 - (embedding <=> %s) AS similarity,
               EXP(-EXTRACT(EPOCH FROM (now() - last_access_at))
                   / 86400.0 / stability) AS decay,
               access_count, stability, last_access_at, created_at
        FROM memories
        {where_clause}
        ORDER BY embedding <=> %s
        LIMIT %s
    ),
    scored AS (
        SELECT *, (similarity * decay) AS score
        FROM hits
        WHERE (similarity * decay) > %s
    ),
    ranked AS (
        SELECT * FROM scored ORDER BY score DESC LIMIT %s
    ),
    updated AS (
        UPDATE memories m
        SET access_count = m.access_count + 1,
            stability = LEAST(m.stability * 1.7, 365),
            last_access_at = now()
        FROM ranked
        WHERE m.id = ranked.id
        RETURNING m.id
    )
    SELECT r.id, r.kind, r.content, r.source_path, r.source_section,
           r.metadata, r.similarity, r.decay, r.score,
           r.access_count + 1 AS access_count,
           LEAST(r.stability * 1.7, 365) AS stability,
           now() AS last_access_at,
           r.created_at,
           (SELECT COUNT(*) FROM updated) AS _updated_count
    FROM ranked r
    ORDER BY r.score DESC
    """

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            await conn.commit()

    out: list[dict] = []
    for row in rows:
        (rid, rkind, content, source_path, source_section, metadata,
         similarity, decay, score, access_count, stability,
         last_access_at, created_at, _updated_count) = row
        out.append({
            "id": str(rid),
            "kind": rkind,
            "content": content,
            "source_path": source_path,
            "source_section": source_section,
            "metadata": metadata or {},
            "similarity": float(similarity) if similarity is not None else None,
            "decay": float(decay) if decay is not None else None,
            "score": float(score) if score is not None else None,
            "access_count": int(access_count),
            "stability": float(stability),
            "last_access_at": last_access_at.isoformat() if last_access_at else None,
            "created_at": created_at.isoformat() if created_at else None,
        })

    # Fire-and-forget audit for each reinforced hit. audit.record() itself
    # gates on AUDIT_REINFORCE env, but we also skip task creation entirely
    # when disabled to avoid needless event-loop churn.
    if out and audit._audit_reinforce_enabled():
        for hit in out:
            asyncio.create_task(
                audit.record(
                    "reinforce",
                    subject_id=hit["id"],
                    actor="store:search",
                    kind=hit.get("kind"),
                    content=hit.get("content"),
                    metadata={"query": query, "score": hit.get("score")},
                )
            )
    return out


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------

async def forget(memory_id: str, *, actor: str = "mcp:forget") -> dict:
    if not db.is_enabled():
        return {"deleted": False}
    snapshot_kind: Optional[str] = None
    snapshot_content: Optional[str] = None
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            # Snapshot BEFORE delete so audit retains the trail.
            await cur.execute(
                "SELECT kind::text, content FROM memories WHERE id = %s",
                (memory_id,),
            )
            srow = await cur.fetchone()
            if srow:
                snapshot_kind = srow[0]
                snapshot_content = srow[1]
            await cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            deleted = cur.rowcount > 0
            await conn.commit()

    if deleted:
        await audit.record(
            "delete",
            subject_id=memory_id,
            actor=actor,
            kind=snapshot_kind,
            content=snapshot_content,
            metadata={"deleted_at": "now()"},
        )
    return {"deleted": bool(deleted)}


# ---------------------------------------------------------------------------
# count_by_kind
# ---------------------------------------------------------------------------

async def count_by_kind() -> dict[str, int]:
    if not db.is_enabled():
        return {"doc_chunk": 0, "interaction": 0, "insight": 0, "total": 0}
    out = {"doc_chunk": 0, "interaction": 0, "insight": 0, "total": 0}
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind::text, COUNT(*) FROM memories GROUP BY kind"
            )
            rows = await cur.fetchall()
    for k, c in rows:
        out[k] = int(c)
        out["total"] += int(c)
    return out
