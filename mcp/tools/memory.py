"""Semantic memory tools for Aleph Docs MCP."""

import logging

from pathlib import Path

from helpers import error_response
from memory import db, store
from memory import media as _media
from memory.chunker_audio import chunk_audio as _chunk_audio
from memory.chunker_image import chunk_image as _chunk_image
from memory.chunker_pdf import chunk_pdf as _chunk_pdf
from memory.chunker_video import chunk_video as _chunk_video
from memory.types import MediaChunk

log = logging.getLogger("memory")


# Extension → (modality label, lazy chunker). Only images are wired in
# Wave 2A; video/audio/pdf raise NotImplementedError with a pointer to
# the wave that enables them. Agents B/C replace those branches with
# real chunkers without touching the surrounding tool.
_MEDIA_ROUTES: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".pdf": "pdf",
}


def _route_media(path: Path, *, caption: str | None = None) -> list[MediaChunk]:
    """Dispatch a media file to its chunker based on extension.

    Returns a list of MediaChunks — images and PDFs naturally produce 1
    or more, videos/audio may produce many segments. The caller
    (`remember_media`) iterates and upserts each chunk.
    """
    suffix = path.suffix.lower()
    modality = _MEDIA_ROUTES.get(suffix)
    if modality is None:
        raise ValueError(
            f"extension {suffix!r} is not a recognised media type. "
            f"Allowed: {sorted(_MEDIA_ROUTES.keys())}"
        )
    if modality == "image":
        return [_chunk_image(path, caption=caption)]
    if modality == "video":
        # Segment files must outlive every `store.upsert_media_chunk` call
        # (the backend reads them during the embed step). `mkdtemp` returns
        # a directory that survives the function; the OS /tmp reaper
        # eventually cleans it. We stash the path in metadata so callers
        # can optionally remove it after all upserts complete.
        import tempfile as _tempfile
        tmpdir = _tempfile.mkdtemp(prefix="aleph-video-")
        chunks = _chunk_video(path, out_dir=Path(tmpdir), caption=caption)
        if not chunks:
            raise RuntimeError(f"chunk_video returned no scenes for {path}")
        for c in chunks:
            c.metadata.setdefault("_tmpdir", tmpdir)
        return chunks
    if modality == "audio":
        import tempfile as _tempfile
        tmpdir = _tempfile.mkdtemp(prefix="aleph-audio-")
        chunks = _chunk_audio(path, out_dir=Path(tmpdir), transcript=caption)
        if not chunks:
            raise RuntimeError(f"chunk_audio returned no clips for {path}")
        for c in chunks:
            c.metadata.setdefault("_tmpdir", tmpdir)
        return chunks
    if modality == "pdf":
        chunks = _chunk_pdf(path)
        if not chunks:
            raise RuntimeError(f"chunk_pdf returned no pages for {path}")
        return chunks
    raise ValueError(f"unhandled modality: {modality}")


def register(mcp):
    @mcp.tool()
    async def semantic_search(query: str, kind: str | None = None,
                              limit: int = 10, min_score: float = 0.15) -> dict:
        """Semantic vector search across the unified memory (docs, interactions, insights).

        Returns results ranked by similarity x forgetting-curve decay. Each hit
        is reinforced atomically (access_count, stability bumped).

        Args:
            query: Natural-language query (sentence or keywords work equally).
            kind: Optional filter - 'doc_chunk', 'interaction', or 'insight'.
            limit: Max results (1-50, default 10).
            min_score: Filter out results below this score (default 0.15).
        """
        try:
            limit = max(1, min(int(limit), 50))
            if kind and kind not in ("doc_chunk", "interaction", "insight"):
                return {"error": f"invalid kind: {kind}"}
            results = await store.search(query, kind=kind, limit=limit, min_score=min_score)
            return {"query": query, "kind": kind, "count": len(results), "results": results}
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled (set MEMORY_ENABLED=true + PG_DSN)"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def remember(content: str, context: str = "",
                       source_path: str | None = None,
                       tags: list[str] | None = None) -> dict:
        """Save an explicit insight to long-term memory.

        Use during support work when you discover something worth remembering
        across sessions (gotcha, workaround, customer-specific answer).

        Args:
            content: The insight text (1-3 sentences is ideal).
            context: Optional surrounding context (ticket URL, customer, etc).
            source_path: Optional canonical doc path this insight relates to.
            tags: Optional list of string tags for later filtering.
        """
        if not content or not content.strip():
            return {"error": "content must not be empty"}
        try:
            return await store.insert_insight(content.strip(), context, source_path, tags)
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def remember_media(path: str, context: str = "",
                             caption: str | None = None,
                             tags: list[str] | None = None) -> dict:
        """Embed a local media file as an insight-like memory.

        Phase 1 wires only images (PNG/JPEG/WEBP). Video/audio/PDF are
        stubbed out with a clear NotImplementedError and will be enabled
        in Waves 2B/2C.

        Requires the active embedder backend to support the modality (e.g.
        `EMBED_BACKEND=gemini-2-preview`). Without a multimodal backend,
        the call returns a structured error instead of silently failing.

        Args:
            path: Absolute path to a media file.
            context: Free-form note (ticket URL, customer, etc).
            caption: Optional human caption; becomes the `content` field.
            tags: Optional list of string tags.
        """
        from pathlib import Path as _Path

        if not path or not path.strip():
            return {"error": "path must not be empty"}
        p = _Path(path)
        if not p.is_absolute():
            return {"error": f"path must be absolute: {path}"}
        if not p.is_file():
            return {"error": f"file not found: {path}"}

        try:
            chunks = _route_media(p, caption=caption)
            inserted = []
            for chunk in chunks:
                res = await store.upsert_media_chunk(
                    chunk, context=context, tags=tags,
                )
                inserted.append(res)
            # Single-modality summary shape.
            return {
                "count": len(inserted),
                "kind": inserted[0].get("kind") if inserted else None,
                "media_type": inserted[0].get("media_type") if inserted else None,
                "ids": [r.get("id") for r in inserted],
                # For single-chunk media (images) the top-level `id` field
                # preserves the old single-insert response shape.
                "id": inserted[0].get("id") if len(inserted) == 1 else None,
            }
        except NotImplementedError as e:
            return {"error": str(e)}
        except ValueError as e:
            return {"error": f"invalid media: {e}"}
        except RuntimeError as e:
            return {"error": str(e)}
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def recall(query: str, limit: int = 10) -> dict:
        """Search only your accumulated insights + past interactions.

        Alias of `semantic_search` filtered to exclude doc_chunks - useful to
        surface prior answers or explicit notes without docs noise.

        Args:
            query: Natural-language query.
            limit: Max results (default 10).
        """
        try:
            limit = max(1, min(int(limit), 50))
            insights = await store.search(query, kind="insight", limit=limit, min_score=0.15)
            interactions = await store.search(query, kind="interaction", limit=limit, min_score=0.15)
            merged = sorted(insights + interactions, key=lambda r: r.get("score", 0.0), reverse=True)[:limit]
            return {"query": query, "count": len(merged), "results": merged}
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def suggest_doc_update(topic: str, top_k: int = 8) -> dict:
        """Propose a markdown patch to the canonical docs based on stored memory.

        Algorithm (PRD 3.6):
        1) Fetch top-k insights+interactions matching `topic` (score > 0.3).
        2) Fetch top-k doc_chunks; aggregate score per source_path; pick best.
        3) Compose a '## Note dal supporto (auto-suggerite)' block listing the
           insights with stability/access_count annotation.
        4) Return a structured suggestion. NO automatic PR.

        Args:
            topic: The topic to generate suggestions for.
            top_k: How many supporting memories to consider (default 8).
        """
        try:
            top_k = max(1, min(int(top_k), 20))
            insights = await store.search(topic, kind="insight", limit=top_k, min_score=0.3)
            interactions = await store.search(topic, kind="interaction", limit=top_k, min_score=0.3)
            supporting = sorted(insights + interactions, key=lambda r: r.get("score", 0.0), reverse=True)[:top_k]
            if not supporting:
                return {"topic": topic, "target_path": None,
                        "confidence": 0.0, "related_insights": [],
                        "suggested_diff_markdown": "",
                        "message": "no memory above threshold - nothing to suggest yet"}

            doc_hits = await store.search(topic, kind="doc_chunk", limit=top_k, min_score=0.15)
            agg: dict[str, float] = {}
            section_of: dict[str, str] = {}
            for d in doc_hits:
                sp = d.get("source_path") or ""
                if not sp:
                    continue
                agg[sp] = agg.get(sp, 0.0) + d.get("score", 0.0)
                section_of.setdefault(sp, d.get("source_section") or "")
            target_path = max(agg, key=agg.get) if agg else None
            target_section = section_of.get(target_path or "", "")

            lines = ["## Note dal supporto (auto-suggerite)", ""]
            for m in supporting:
                c = (m.get("content") or "").replace("\n", " ").strip()
                s = m.get("stability", 0.0)
                n = m.get("access_count", 0)
                lines.append(f"- {c} _(stability: {s:.1f}, access_count: {n})_")
            suggested = "\n".join(lines) + "\n"

            confidence = min(1.0, (sum(m.get("score", 0.0) for m in supporting) / len(supporting)) if supporting else 0.0)
            return {
                "topic": topic,
                "target_path": target_path,
                "target_section": target_section,
                "suggested_diff_markdown": suggested,
                "related_insights": [
                    {"id": m["id"], "content": m["content"], "score": m.get("score"),
                     "stability": m.get("stability"), "access_count": m.get("access_count")}
                    for m in supporting
                ],
                "confidence": round(confidence, 3),
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def propose_doc_patch(topic: str, top_k: int = 8,
                                dry_run: bool = False,
                                open_pr: bool = False) -> dict:
        """Prepare a git branch + commit in the docs repo with a suggested update.

        Wraps `suggest_doc_update`: runs the same algorithm, then (if confidence
        is high enough) creates a local branch `docs/mcp-<slug>-<YYYYMMDD-HHMM>`
        inside the docs repo clone, inserts the suggested markdown block after
        the target H2 section (or at EOF), and commits it.

        When `open_pr=True` it also pushes the branch and opens a GitHub pull
        request against `main` (requires DOCS_WRITE_TOKEN env with write scope).

        Args:
            topic: Topic to generate a suggestion for (same as suggest_doc_update).
            top_k: How many supporting memories to consider (default 8).
            dry_run: If true, compute the plan but don't touch the repo.
            open_pr: If true, after committing also push the branch and open a PR.
        """
        from memory.doc_patch import apply_patch, push_branch, open_pull_request, _get_repo_path

        try:
            top_k = max(1, min(int(top_k), 20))
            insights = await store.search(topic, kind="insight",
                                          limit=top_k, min_score=0.3)
            interactions = await store.search(topic, kind="interaction",
                                              limit=top_k, min_score=0.3)
            supporting = sorted(insights + interactions,
                                key=lambda r: r.get("score", 0.0),
                                reverse=True)[:top_k]
            if not supporting:
                return {"status": "skipped", "topic": topic,
                        "confidence": 0.0,
                        "message": "no memory above threshold - nothing to suggest"}

            doc_hits = await store.search(topic, kind="doc_chunk",
                                          limit=top_k, min_score=0.15)
            agg: dict[str, float] = {}
            section_of: dict[str, str] = {}
            for d in doc_hits:
                sp = d.get("source_path") or ""
                if not sp:
                    continue
                agg[sp] = agg.get(sp, 0.0) + d.get("score", 0.0)
                section_of.setdefault(sp, d.get("source_section") or "")
            target_path = max(agg, key=agg.get) if agg else None
            target_section = section_of.get(target_path or "", "")

            confidence = min(
                1.0,
                sum(m.get("score", 0.0) for m in supporting) / len(supporting),
            )

            if not target_path or confidence < 0.3:
                return {
                    "status": "skipped",
                    "topic": topic,
                    "target_path": target_path,
                    "confidence": round(confidence, 3),
                    "message": ("no target_path" if not target_path
                                else "confidence below 0.3"),
                }

            lines = ["## Note dal supporto (auto-suggerite)", ""]
            for m in supporting:
                c = (m.get("content") or "").replace("\n", " ").strip()
                s = m.get("stability", 0.0)
                n = m.get("access_count", 0)
                lines.append(f"- {c} _(stability: {s:.1f}, access_count: {n})_")
            suggested = "\n".join(lines) + "\n"

            commit_subject = f"docs: auto-suggestion for {topic}"
            body_lines = [
                f"Topic: {topic}",
                f"Target: {target_path} (section: {target_section or 'EOF'})",
                f"Confidence: {confidence:.3f}",
                "",
                "Supporting insights:",
            ]
            for m in supporting:
                preview = (m.get("content") or "").replace("\n", " ")[:180]
                body_lines.append(
                    f"- [{m.get('id')}] score={m.get('score', 0):.2f} "
                    f"stability={m.get('stability', 0):.1f} "
                    f"access_count={m.get('access_count', 0)}: {preview}"
                )
            commit_body = "\n".join(body_lines)

            supporting_payload = [
                {"id": m["id"], "content": m["content"],
                 "score": m.get("score"),
                 "stability": m.get("stability"),
                 "access_count": m.get("access_count")}
                for m in supporting
            ]

            result = apply_patch(
                topic=topic,
                target_rel_path=target_path,
                section_anchor=target_section,
                markdown_block=suggested,
                commit_message_subject=commit_subject,
                commit_message_body=commit_body,
                dry_run=bool(dry_run),
            )

            out = result.to_dict()
            out.update({
                "topic": topic,
                "target_path": target_path,
                "target_section": target_section,
                "confidence": round(confidence, 3),
                "supporting_insights": supporting_payload,
            })

            # Optional: push + open GitHub PR.
            if open_pr and result.status == "committed" and result.branch:
                try:
                    repo_path = _get_repo_path()
                    push_branch(repo_path, result.branch)
                    out["pushed"] = True
                    pr_body = f"Auto-suggestion from aleph-docs-mcp MCP.\n\n{commit_body}"
                    pr_url = open_pull_request(
                        branch=result.branch,
                        title=commit_subject,
                        body=pr_body,
                    )
                    out["pr_url"] = pr_url
                    out["next_steps"] = f"Review the PR at {pr_url}"
                except Exception as e:
                    log.warning("[memory] open_pr failed: %s", e)
                    out["pr_error"] = f"{type(e).__name__}: {e}"
                    out["next_steps"] = (
                        f"Branch {result.branch} committed locally but push/PR "
                        f"failed: {e}. You can retry manually."
                    )
            elif result.branch:
                out["next_steps"] = (
                    f"cd <repo> && git diff main...{result.branch}   "
                    "# review\n"
                    f"Set open_pr=true on the next call to push + open a PR."
                )
            return out
        except db.MemoryDisabled:
            return {"status": "error",
                    "error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def forget(memory_id: str) -> dict:
        """Delete a memory entry by its UUID.

        Args:
            memory_id: The UUID of the memory to delete.
        """
        try:
            return await store.forget(memory_id)
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def lint_run(mode: str = "auto") -> dict:
        """Trigger an on-demand memory lint run.

        Modes:
          'auto'   - smart (default): skip if idle, downgrade to cheap if a
                     full ran within LINT_FULL_INTERVAL_HOURS.
          'cheap'  - free checks only (orphan, redundant, stale).
          'full'   - cheap + LLM contradiction judge.
          'manual' - same as 'full' but never skip.

        Returns: {run_id, mode_used, findings_count, tokens_used, cost_usd,
                  skipped, duration_seconds}.
        """
        try:
            if mode not in ("auto", "cheap", "full", "manual"):
                return {"error": f"invalid mode: {mode}"}
            from memory import lint as _lint
            import os as _os
            from pathlib import Path as _Path
            repo_path_str = _os.environ.get("DOCS_REPO_PATH", "").strip()
            repo_path = _Path(repo_path_str).resolve() if repo_path_str else None
            return await _lint.run_lint(mode=mode, repo_path=repo_path)
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def lint_findings(kind: str | None = None,
                            include_resolved: bool = False,
                            limit: int = 50) -> dict:
        """List memory lint findings.

        Args:
            kind: filter by 'orphan'|'redundant'|'contradiction'|'stale' or None for all.
            include_resolved: show resolved findings too (default false).
            limit: max rows (1-500, default 50).
        """
        try:
            limit = max(1, min(int(limit), 500))
            if kind is not None and kind not in (
                "orphan", "redundant", "contradiction", "stale"
            ):
                return {"error": f"invalid kind: {kind}"}

            clauses: list[str] = []
            params: list = []
            if kind:
                clauses.append("kind = %s")
                params.append(kind)
            if not include_resolved:
                clauses.append("resolved_at IS NULL")
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            sql = (
                "SELECT id, ts, kind, severity, subject_id, related_id, "
                "summary, suggestion, resolved_at, resolution_note, metadata "
                f"FROM memory_lint_findings{where} "
                "ORDER BY ts DESC LIMIT %s"
            )
            async with db.get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            out: list[dict] = []
            for r in rows:
                (rid, ts, rkind, severity, sid, rid2, summary, suggestion,
                 resolved_at, resolution_note, metadata) = r
                out.append({
                    "id": int(rid),
                    "ts": ts.isoformat() if ts else None,
                    "kind": rkind,
                    "severity": severity,
                    "subject_id": str(sid) if sid else None,
                    "related_id": str(rid2) if rid2 else None,
                    "summary": summary,
                    "suggestion": suggestion,
                    "resolved_at": resolved_at.isoformat() if resolved_at else None,
                    "resolution_note": resolution_note,
                    "metadata": metadata or {},
                })
            return {
                "count": len(out),
                "filters": {"kind": kind, "include_resolved": include_resolved},
                "findings": out,
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def lint_resolve(finding_id: int, note: str = "") -> dict:
        """Mark a lint finding as resolved (acknowledged)."""
        try:
            fid = int(finding_id)
            async with db.get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE memory_lint_findings "
                        "SET resolved_at = COALESCE(resolved_at, now()), "
                        "    resolution_note = CASE "
                        "      WHEN resolved_at IS NULL THEN %s "
                        "      ELSE resolution_note END "
                        "WHERE id = %s "
                        "RETURNING id, resolved_at, resolution_note",
                        (note or None, fid),
                    )
                    row = await cur.fetchone()
                    await conn.commit()
            if not row:
                return {"error": f"finding {fid} not found"}
            return {
                "id": int(row[0]),
                "resolved_at": row[1].isoformat() if row[1] else None,
                "resolution_note": row[2],
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def audit_history(
        subject_id: str | None = None,
        op: str | None = None,
        since_hours: int = 168,
        limit: int = 50,
    ) -> dict:
        """Return recent audit events from the memory system.

        Args:
            subject_id: Filter to a specific memory UUID (shows its full history).
            op: Filter by operation ('insert', 'update', 'delete', 'reinforce').
            since_hours: How far back to look (default 168 = 1 week).
            limit: Max rows to return (1-500, default 50).
        """
        try:
            limit = max(1, min(int(limit), 500))
            since_hours = max(0, int(since_hours)) if since_hours is not None else 168
            if op is not None and op not in ("insert", "update", "delete", "reinforce", "access"):
                return {"error": f"invalid op: {op}"}

            clauses = ["ts >= now() - (%s || ' hours')::interval"]
            params: list = [str(since_hours)]
            if subject_id:
                clauses.append("subject_id = %s")
                params.append(subject_id)
            if op:
                clauses.append("op = %s")
                params.append(op)
            where = " AND ".join(clauses)
            params.append(limit)

            sql = (
                "SELECT id, ts, op, subject_id, actor, kind, content, metadata "
                f"FROM memory_audit WHERE {where} "
                "ORDER BY ts DESC LIMIT %s"
            )

            async with db.get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()

            out: list[dict] = []
            for r in rows:
                rid, ts, rop, sid, actor, kind, content, metadata = r
                out.append({
                    "id": int(rid),
                    "ts": ts.isoformat() if ts else None,
                    "op": rop,
                    "subject_id": str(sid) if sid else None,
                    "actor": actor,
                    "kind": kind,
                    "content": (content or "")[:200],
                    "metadata": metadata or {},
                })
            return {
                "count": len(out),
                "filters": {"subject_id": subject_id, "op": op, "since_hours": since_hours},
                "events": out,
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)
