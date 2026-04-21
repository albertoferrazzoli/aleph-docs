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
# `*_transcript` kinds are text-embedded (the transcript string IS the
# embed input) — they pass through any backend that supports text.
#
# Backend / kind compatibility notes:
#   gemini-*              : all kinds supported.
#   local  (Ollama text)  : only *_transcript + pdf_text (text-only).
#   nomic_multimodal_local: text kinds + `image` (via vision endpoint).
#       video_scene / audio_clip / pdf_page WILL fail fast here because
#       the backend does not declare video/audio/pdf modalities — by
#       design. For video coverage we emit extra `kind="image"` chunks
#       from extracted keyframes (see chunker_video.py); audio stays
#       covered by audio_transcript; PDFs stay covered by pdf_text.
_KIND_TO_MODALITY = {
    "image": "image",
    "video_scene": "video",
    "audio_clip": "audio",
    "pdf_page": "pdf",
    "video_transcript": "text",
    "audio_transcript": "text",
    "pdf_text": "text",
}

# Kinds that embed `chunk.content` (a transcript string) instead of
# `chunk.path` (a media file). The path field is expected to be None.
_TEXT_EMBEDDING_KINDS = frozenset({
    "video_transcript", "audio_transcript", "pdf_text",
})


async def upsert_media_chunk(
    chunk: MediaChunk,
    *,
    actor: str = "mcp:remember_media",
    context: str = "",
    tags: list[str] | None = None,
    source_path: str | None = None,
    source_sha256: str | None = None,
    source_mtime: int | None = None,
) -> dict:
    """Embed + insert a single media memory row.

    Validates that the active embedder backend supports the modality
    required by `chunk.kind` BEFORE spending any tokens. If not, raises
    RuntimeError with the active backend name — the tool layer surfaces
    this as a structured error so callers know to switch backends.

    When `source_path` is provided, it is persisted in the `source_path`
    column so the reconciler can diff filesystem state against DB state
    and locate all chunks sharing the same input file. When omitted,
    falls back to the base of `chunk.media_ref` (stripping any
    `#page=`/`#t=` fragment). `source_sha256` / `source_mtime` go into
    metadata for idempotency checks on re-ingest.

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

    # Early dedup short-circuit: check if a row with this media_ref
    # already exists BEFORE paying the embed cost. For the md-image
    # rebuild case this avoids 150+ Nomic HTTP calls per workspace
    # switch — and gets rid of the UPDATE+notify spam the viewer
    # renders as "reinforce" badges.
    if chunk.media_ref:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, metadata->>'source_sha256' "
                    "FROM memories "
                    "WHERE kind = %s AND media_ref = %s "
                    "LIMIT 1",
                    (chunk.kind, chunk.media_ref),
                )
                pre = await cur.fetchone()
        if pre is not None:
            pre_id, pre_sha = pre
            # Sha match OR caller didn't provide a sha (md-image path):
            # row is considered unchanged, skip embed + any write.
            if (source_sha256 and pre_sha == source_sha256) or not source_sha256:
                return {
                    "id": str(pre_id),
                    "kind": chunk.kind,
                    "media_type": chunk.media_type,
                    "deduplicated": True,
                }
            # Sha mismatch → we'll embed and UPDATE below (Case C).

    # Text-embedded kinds (*_transcript) pass the transcript string to
    # the embedder; media kinds pass the file path so the backend reads
    # bytes itself.
    if chunk.kind in _TEXT_EMBEDDING_KINDS:
        if not (chunk.content and chunk.content.strip()):
            raise RuntimeError(
                f"upsert_media_chunk: chunk.content is empty for "
                f"text-embedded kind={chunk.kind!r}"
            )
        embed_input = chunk.content
    else:
        if chunk.path is None:
            raise RuntimeError(
                f"upsert_media_chunk: chunk.path is None for kind={chunk.kind!r}"
            )
        embed_input = chunk.path

    # Embed via the shared embeddings shim so tests can monkeypatch it.
    # It forwards to the active backend with EMBED_DIM guarding.
    vectors = await embeddings.embed_batch([embed_input])
    if not vectors:
        raise RuntimeError("upsert_media_chunk: backend returned no vectors")
    emb = vectors[0]

    stability = _stability_for("insight")  # media ≈ insight for now
    meta = dict(chunk.metadata or {})
    if context:
        meta["context"] = context
    if tags:
        meta["tags"] = list(tags)
    if source_sha256:
        meta["source_sha256"] = source_sha256
    if source_mtime is not None:
        meta["source_mtime"] = int(source_mtime)

    # Resolve the canonical `source_path` used for reconciliation. Prefer
    # the explicit arg; fall back to the base of media_ref (strip fragment).
    if source_path:
        resolved_source_path = source_path
    else:
        resolved_source_path = (chunk.media_ref or "").split("#", 1)[0] or None

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            # Idempotency: if a row for the same (kind, media_ref) tuple
            # already exists AND the source hash is unchanged, reuse it
            # instead of inserting a duplicate. Without this guard every
            # markdown rebuild multiplies image rows once per referencing
            # .md file, per rebuild — a 178-image corpus balloons to
            # thousands of rows in a few switches.
            if chunk.media_ref:
                await cur.execute(
                    "SELECT id, metadata->>'source_sha256' "
                    "FROM memories "
                    "WHERE kind = %s AND media_ref = %s "
                    "LIMIT 1",
                    (chunk.kind, chunk.media_ref),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    existing_id, existing_sha = existing
                    # Case A: explicit sha match → no-op.
                    if source_sha256 and existing_sha == source_sha256:
                        return {
                            "id": str(existing_id),
                            "kind": chunk.kind,
                            "media_type": chunk.media_type,
                            "deduplicated": True,
                        }
                    # Case B: caller didn't provide a sha at all (e.g. the
                    # markdown indexer re-walking img refs on every
                    # workspace switch). We have no evidence the file
                    # changed — don't UPDATE the row or it'll fire a
                    # memory_change notify that the viewer renders as
                    # a "reinforce" badge on every switch.
                    if not source_sha256:
                        return {
                            "id": str(existing_id),
                            "kind": chunk.kind,
                            "media_type": chunk.media_type,
                            "deduplicated": True,
                        }
                    # Case C: explicit sha that DIFFERS from the stored
                    # one → file genuinely changed, update the row in
                    # place with the fresh embedding + content + meta.
                    await cur.execute(
                        "UPDATE memories SET "
                        "  content = %s, source_path = %s, "
                        "  metadata = %s::jsonb, embedding = %s, "
                        "  media_type = %s, preview_b64 = %s, "
                        "  last_access_at = now() "
                        "WHERE id = %s",
                        (
                            chunk.content, resolved_source_path,
                            json.dumps(meta), _vec(emb),
                            chunk.media_type, chunk.preview_b64,
                            existing_id,
                        ),
                    )
                    await conn.commit()
                    new_id = str(existing_id)
                    await audit.record(
                        "update",
                        subject_id=new_id,
                        actor=actor,
                        kind=chunk.kind,
                        content=chunk.content,
                        metadata={"media_ref": chunk.media_ref},
                    )
                    return {
                        "id": new_id,
                        "kind": chunk.kind,
                        "media_type": chunk.media_type,
                        "updated": True,
                    }

            await cur.execute(
                "INSERT INTO memories "
                "  (kind, content, source_path, metadata, embedding, "
                "   stability, media_ref, media_type, preview_b64) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    chunk.kind,
                    chunk.content,
                    resolved_source_path,
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
# delete_by_source_path — cascade delete all chunks for one input file.
#
# Used by the media reconciler when a file is removed from docs/ or when
# its hash has changed and all old chunks must be wiped before re-ingest.
# Also covers the previously-orphaned .md delete case (one row per doc_chunk
# sharing the same source_path).
# ---------------------------------------------------------------------------

async def delete_by_source_path(
    source_path: str,
    *,
    actor: str = "indexer:reconcile",
    kinds: list[str] | None = None,
) -> list[dict]:
    """Delete every memory whose source_path matches, snapshot to audit.

    Args:
        source_path: Exact value of the `source_path` column to match.
        actor: Audit actor label.
        kinds: Optional restriction to specific kinds (default: all).

    Returns a list of {id, kind, content} dicts for the rows that were
    deleted (so callers can log per-chunk what went away).
    """
    if not db.is_enabled():
        return []
    snapshots: list[tuple] = []
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            if kinds:
                await cur.execute(
                    "SELECT id, kind::text, content FROM memories "
                    "WHERE source_path = %s AND kind::text = ANY(%s)",
                    (source_path, list(kinds)),
                )
            else:
                await cur.execute(
                    "SELECT id, kind::text, content FROM memories "
                    "WHERE source_path = %s",
                    (source_path,),
                )
            snapshots = await cur.fetchall() or []
            if not snapshots:
                return []
            ids = [row[0] for row in snapshots]
            await cur.execute(
                "DELETE FROM memories WHERE id = ANY(%s)",
                (ids,),
            )
            await conn.commit()

    out: list[dict] = []
    for (mid, kind, content) in snapshots:
        await audit.record(
            "delete",
            subject_id=str(mid),
            actor=actor,
            kind=kind,
            content=content,
            metadata={"source_path": source_path, "reason": "reconcile"},
        )
        out.append({"id": str(mid), "kind": kind, "content": content})
    log.info(
        "[memory] delete_by_source_path %s: removed %d row(s)",
        source_path, len(out),
    )
    return out


# ---------------------------------------------------------------------------
# list_media_source_paths_with_hash — DB-side state for the reconciler diff.
# ---------------------------------------------------------------------------

_MEDIA_KINDS_SQL = (
    "image", "video_scene", "audio_clip", "pdf_page",
    # Text-embedded media kinds must be tracked here too — otherwise
    # the reconciler can't tell that e.g. a video is "already done in
    # HYBRID=false mode" (video_transcript-only) and re-ingests it
    # every run, creating duplicates.
    "video_transcript", "audio_transcript", "pdf_text",
)


async def list_media_source_paths_with_hash() -> dict[str, dict]:
    """Return `{source_path: {sha256, mtime, kinds, count}}` over media rows.

    The reconciler compares this map against the filesystem to decide
    add/update/delete. `sha256` is taken from metadata['source_sha256']
    of the first chunk for a given path (all chunks of the same file
    share the same source sha). `kinds` is the distinct set of kinds
    seen (useful for diagnostics).
    """
    if not db.is_enabled():
        return {}
    out: dict[str, dict] = {}
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT source_path, "
                "       (metadata->>'source_sha256') AS sha256, "
                "       (metadata->>'source_mtime') AS mtime, "
                "       kind::text, COUNT(*) AS n "
                "FROM memories "
                "WHERE source_path IS NOT NULL "
                "  AND kind::text = ANY(%s) "
                "GROUP BY source_path, "
                "         (metadata->>'source_sha256'), "
                "         (metadata->>'source_mtime'), kind::text",
                (list(_MEDIA_KINDS_SQL),),
            )
            rows = await cur.fetchall() or []
    for (sp, sha, mt, kind, n) in rows:
        entry = out.setdefault(
            sp, {"sha256": sha, "mtime": mt, "kinds": set(), "count": 0}
        )
        # If multiple rows disagree on sha256/mtime (e.g. a partially-migrated
        # corpus) prefer the non-null one so downstream idempotency still works.
        if not entry.get("sha256") and sha:
            entry["sha256"] = sha
        if not entry.get("mtime") and mt:
            entry["mtime"] = mt
        entry["kinds"].add(kind)
        entry["count"] += int(n)
    for v in out.values():
        v["kinds"] = sorted(v["kinds"])
    return out


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
               CASE
                   WHEN kind IN ('doc_chunk','image','pdf_page','video_scene','audio_clip')
                       THEN 1.0
                   ELSE EXP(-EXTRACT(EPOCH FROM (now() - last_access_at))
                            / 86400.0 / stability)
               END AS decay,
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
