"""Semantic memory tools for Aleph Docs MCP."""

import base64
import logging
import os
import re

from pathlib import Path

from fastmcp.utilities.types import Image

from helpers import error_response
from memory import db, store
from memory import media as _media
from memory.chunker_audio import chunk_audio as _chunk_audio
from memory.chunker_image import chunk_image as _chunk_image
from memory.chunker_pdf import chunk_pdf as _chunk_pdf
from memory.chunker_video import chunk_video as _chunk_video
from memory.types import MediaChunk

log = logging.getLogger("memory")

_MEDIA_KINDS = frozenset({"image", "pdf_page", "video_scene", "audio_clip"})
_ALL_KINDS = frozenset({"doc_chunk", "interaction", "insight"}) | _MEDIA_KINDS


def _slug_for_new_file(text: str, max_len: int = 60) -> str:
    """Lowercase ASCII slug suitable for a filesystem path. Collapses
    non-alphanumerics into single hyphens; caps length so the file name
    stays readable."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def _title_from_topic(text: str) -> str:
    """Cleaned topic string suitable as H1. Keeps original casing but
    trims whitespace and drops trailing punctuation."""
    t = text.strip().rstrip(".!?,;:")
    return t or "Untitled"


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
        """Semantic vector search across the unified memory.

        Covers all 7 memory kinds: doc_chunk, interaction, insight (text),
        and image, video_scene, audio_clip, pdf_page (media). Results are
        ranked by similarity x forgetting-curve decay. Each hit is
        reinforced atomically.

        NOTE on min_score for cross-modal queries: text->image/video/audio
        cosine similarities run lower (~0.2–0.45) than text->text
        (~0.4–0.85). For mixed-modality queries keep min_score at 0.15
        and let the ranking sort itself out; do NOT raise to 0.5 or
        media hits will all be filtered out.

        Args:
            query: Natural-language query.
            kind: Optional filter - one of:
                'doc_chunk', 'interaction', 'insight',
                'image', 'video_scene', 'audio_clip', 'pdf_page'.
            limit: Max results (1-50, default 10).
            min_score: Minimum score to include (default 0.15).
        """
        try:
            limit = max(1, min(int(limit), 50))
            if kind and kind not in _ALL_KINDS:
                return {"error": f"invalid kind: {kind}. allowed: {sorted(_ALL_KINDS)}"}
            results = await store.search(query, kind=kind, limit=limit, min_score=min_score)
            return {"query": query, "kind": kind, "count": len(results), "results": results}
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled (set MEMORY_ENABLED=true + PG_DSN)"}
        except Exception as e:
            return error_response(e)

    async def _fetch_preview(memory_id: str) -> tuple[bytes | None, str | None, dict | None]:
        """Return (png_bytes, media_type, row_meta) for a memory id.

        - If preview_b64 is stored, decode it (fast, 20KB).
        - Else: return (None, media_type, row_meta) so callers can fall back
          to full-res extraction via the media endpoint.
        """
        if not db.is_enabled():
            return None, None, None
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT kind::text, content, source_path, media_ref, "
                    "       media_type, preview_b64 "
                    "FROM memories WHERE id = %s",
                    (memory_id,),
                )
                row = await cur.fetchone()
        if not row:
            return None, None, None
        kind, content, sp, media_ref, media_type, preview_b64 = row
        meta = {"kind": kind, "content": content, "source_path": sp,
                "media_ref": media_ref, "media_type": media_type}
        if not preview_b64:
            return None, media_type, meta
        try:
            data = base64.b64decode(preview_b64)
        except Exception:
            return None, media_type, meta
        return data, media_type or "image/jpeg", meta

    @mcp.tool(output_schema=None)
    async def search_images(query: str, limit: int = 8,
                            min_score: float = 0.15):
        """Search for visual chunks (images, PDF pages, video scenes).

        IMPORTANT — how to present these results to the user:
        Each hit comes with TWO clickable URLs:
          • `open preview` → opens the JPEG thumbnail in the browser
          • `open source`  → opens the original PDF / video / image
        YOU MUST surface these links verbatim in your reply — do NOT
        paraphrase them away. The user often can't see the inline
        thumbnails that the client may filter; the links are the
        reliable fallback. Keep the markdown headings, scores, and
        links from this tool's output as-is in your reply, then add
        your commentary/analysis BELOW them.

        Response structure:
          1. markdown text: one section per hit with score, source,
             inline data-URI preview, and two clickable URLs
          2. native MCP Image blocks: one per hit (rendered by
             Claude Code / MCP Inspector; invisible in Claude Desktop)

        Args:
            query: Natural-language query (e.g. "candle wick sweep above highs").
            limit: Max images to return (1-20, default 8).
            min_score: Minimum score (default 0.15 — do NOT raise
                above 0.4 for cross-modal queries or you lose hits).
        """
        try:
            limit = max(1, min(int(limit), 20))
            # Search across the three preview-capable kinds and merge.
            per_kind = 4
            hits = []
            for k in ("image", "pdf_page", "video_scene"):
                rs = await store.search(query, kind=k, limit=per_kind, min_score=min_score)
                hits.extend(rs)
            hits.sort(key=lambda r: r.get("score", 0), reverse=True)
            hits = hits[:limit]

            if not hits:
                return [f'no visual hits for "{query}" above score {min_score}']

            # Response shape: markdown text (renders as <img> in Claude
            # Desktop) PLUS native MCP Image blocks (for clients that
            # render those — Claude Code, MCP Inspector, etc).
            #
            # Why both: as of late 2026 Claude Desktop passes MCP image
            # blocks to the model but does NOT render them visually in
            # the chat thread. Markdown data URIs render reliably.
            # Aleph backend URL for clickable browser links. Overridable
            # via env — when running behind a reverse proxy set
            # ALEPH_PUBLIC_URL=https://your-domain.example/aleph/api
            import os as _os
            aleph_base = _os.environ.get(
                "ALEPH_PUBLIC_URL", "http://localhost:8765/aleph/api"
            )

            md_lines = [
                f'## top {len(hits)} visual hits for "{query}"',
                "",
                "*When relaying to the user, keep the `open preview` / "
                "`open source` links below verbatim — they are the user's "
                "only way to actually see the chart.*",
                "",
            ]
            image_blocks: list = []
            for i, h in enumerate(hits, 1):
                mid = h.get("id")
                score = h.get("score", 0)
                kind = h.get("kind", "?")
                src = h.get("source_path") or h.get("media_ref") or "?"
                data, mime, _ = await _fetch_preview(mid)
                preview_url = f"{aleph_base}/preview/{mid}"
                media_url = f"{aleph_base}/media/{mid}"
                header = f"**{i}.** `{kind}` · score `{score:.3f}` · `{src}` · id `{mid}`"
                if data:
                    # Triple-channel delivery so the hit is visible in any MCP client:
                    #  1) markdown data URI  → renders inline in clients that allow it
                    #  2) clickable http URL → works in Claude Desktop (opens browser)
                    #  3) native MCP Image block → Claude Code + spec-compliant clients
                    import base64 as _b64
                    b64 = _b64.b64encode(data).decode()
                    md_lines.append(header)
                    md_lines.append(f"![hit {i}](data:image/jpeg;base64,{b64})")
                    md_lines.append(f"[🔍 open preview]({preview_url}) · [📄 open source]({media_url})")
                    md_lines.append("")
                    image_blocks.append(Image(data=data, format="jpeg"))
                else:
                    md_lines.append(header + " *(no preview)*")
                    md_lines.append(f"[📄 open source]({media_url})")
                    md_lines.append("")
            return ["\n".join(md_lines), *image_blocks]
        except db.MemoryDisabled:
            return ["error: semantic memory is disabled"]
        except Exception as e:
            log.warning("[memory] search_images failed: %s", e)
            return [f"error: {type(e).__name__}: {str(e)[:200]}"]

    @mcp.tool(output_schema=None)
    async def fetch_image(memory_id: str, full_res: bool = False):
        """Return the image of a specific memory as an Image content block.

        Use after semantic_search / search_images when you want to inspect
        one specific hit more closely.

        Args:
            memory_id: UUID of the memory.
            full_res: If true and the memory refers to an image embedded in
                a PDF, re-extract the full-resolution raster from the PDF.
                If false (default), return the stored thumbnail (≤ 20 KB).
        """
        try:
            data, mime, meta = await _fetch_preview(memory_id)
            if not meta:
                return [f"error: memory {memory_id} not found"]
            if full_res and meta.get("kind") == "image" and meta.get("media_ref") and "#page=" in meta["media_ref"]:
                # Re-extract from the source PDF (same logic as aleph /media endpoint).
                import re as _re, io as _io
                m = _re.search(r"#page=(\d+)(?:&img=(\d+))?", meta["media_ref"])
                pdf_path = meta["media_ref"].split("#", 1)[0]
                if m and m.group(2):
                    try:
                        import pypdfium2 as pdfium
                        pdf = pdfium.PdfDocument(pdf_path)
                        try:
                            page = pdf[int(m.group(1)) - 1]
                            try:
                                imgs = list(page.get_objects(
                                    filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,),
                                    max_depth=5,
                                ))
                                idx = int(m.group(2)) - 1
                                if 0 <= idx < len(imgs):
                                    pil = imgs[idx].get_bitmap().to_pil()
                                    buf = _io.BytesIO()
                                    pil.save(buf, format="PNG", optimize=True)
                                    data = buf.getvalue()
                                    mime = "image/png"
                            finally:
                                page.close()
                        finally:
                            pdf.close()
                    except Exception as e:
                        log.warning("[memory] fetch_image full_res failed: %s", e)
            if not data:
                return [
                    f"memory {memory_id} has no preview (kind={meta.get('kind')})"
                ]
            # In full-res path `mime` is image/png (PDF re-extract);
            # in thumbnail path it's the SOURCE media_type (could be
            # application/pdf) but the bytes are a JPEG thumbnail.
            if full_res and mime == "image/png":
                preview_mime, fmt = "image/png", "png"
            else:
                preview_mime, fmt = "image/jpeg", "jpeg"
            import base64 as _b64
            b64 = _b64.b64encode(data).decode()
            md = (
                f"**memory** `{memory_id}`  "
                f"`{meta.get('kind')}`  "
                f"`{meta.get('source_path') or meta.get('media_ref') or ''}`"
                f"\n\n![{meta.get('kind')}](data:{preview_mime};base64,{b64})"
            )
            return [md, Image(data=data, format=fmt)]
        except db.MemoryDisabled:
            return ["error: semantic memory is disabled"]
        except Exception as e:
            log.warning("[memory] fetch_image failed: %s", e)
            return [f"error: {type(e).__name__}: {str(e)[:200]}"]

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
    async def memory_stats() -> dict:
        """Return exact counts of memory rows by kind.

        Use this when the user asks how many memories / docs / images / etc.
        are stored. `semantic_search` is capped at 50 results and doesn't
        expose totals — this tool queries the underlying table directly.
        """
        try:
            counts = await store.count_by_kind()
            total = sum(v for v in counts.values() if isinstance(v, int))
            return {"counts": counts, "total": total}
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def find_doc_gaps(max_top_sim: float = 0.5,
                            limit: int = 10,
                            min_access_count: int = 1) -> dict:
        """List interactions whose best documentation match is weak — gap candidates.

        An interaction represents a real question users asked. If its top
        doc_chunk cosine similarity is low, the docs probably don't cover
        the topic well. Those interactions are the natural seeds for a PR:
        feed each `content` to `suggest_doc_update` as `topic` to decide
        whether to propose an update.

        Args:
            max_top_sim: Gap threshold. Interactions whose top doc_chunk
                similarity is <= this value are considered uncovered. Default 0.5.
            limit: Max interactions returned (default 10).
            min_access_count: Only consider interactions queried at least
                this many times (default 1 = include every interaction).
        """
        try:
            max_top_sim = max(0.0, min(float(max_top_sim), 1.0))
            limit = max(1, min(int(limit), 50))
            async with db.get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        WITH interactions AS (
                            SELECT id, content, access_count, created_at, embedding
                            FROM memories
                            WHERE kind = 'interaction'::memory_kind
                              AND access_count >= %s
                        ),
                        scored AS (
                            SELECT i.id, i.content, i.access_count, i.created_at,
                                   (
                                       SELECT MAX(1 - (d.embedding <=> i.embedding))
                                       FROM memories d
                                       WHERE d.kind = 'doc_chunk'::memory_kind
                                   ) AS top_doc_sim
                            FROM interactions i
                        )
                        SELECT id, content, access_count, created_at, top_doc_sim
                        FROM scored
                        WHERE top_doc_sim IS NULL OR top_doc_sim <= %s
                        ORDER BY access_count DESC, top_doc_sim ASC NULLS FIRST
                        LIMIT %s
                        """,
                        (int(min_access_count), max_top_sim, limit),
                    )
                    rows = await cur.fetchall()
            gaps = [
                {
                    "id": str(r[0]),
                    "topic": (r[1] or "").strip(),
                    "access_count": r[2],
                    "created_at": r[3].isoformat() if r[3] else None,
                    "top_doc_similarity": round(float(r[4]), 3) if r[4] is not None else None,
                }
                for r in rows
            ]
            return {
                "count": len(gaps),
                "max_top_sim_threshold": max_top_sim,
                "gaps": gaps,
                "hint": (
                    "For each gap, call suggest_doc_update(topic=gap.topic) "
                    "to fetch target page + supporting insights; if confident, "
                    "compose prose and invoke propose_doc_patch(prose=...)."
                ),
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def suggest_doc_update(topic: str, top_k: int = 8) -> dict:
        """Collect the raw material needed to extend the canonical docs for a topic.

        This tool does NOT compose the final markdown — it returns structured
        evidence and the caller (the LLM) must weave it into prose that matches
        the voice of the target documentation page.

        Algorithm:
        1) Fetch top-k insights + interactions matching `topic` (score >= 0.3).
        2) Fetch top-k doc_chunks with a strict threshold (score >= 0.4). Only
           pages that contribute at least 2 matching chunks are considered as
           target candidates, to avoid picking a page that merely mentions the
           topic in passing.
        3) Return the best target page, the matching section, the supporting
           notes (content only, no internal metadata), and explicit writing
           instructions for the caller.

        Args:
            topic: The topic to generate suggestions for.
            top_k: How many supporting memories to consider (default 8).
        """
        try:
            top_k = max(1, min(int(top_k), 20))
            # Only insights feed the prose. Interactions are raw query echoes
            # captured by @record_interaction and tend to tautologise the
            # topic rather than add facts; they belong in find_doc_gaps as
            # PR seeds, not in supporting material.
            insights = await store.search(topic, kind="insight", limit=top_k, min_score=0.5)
            supporting = sorted(insights, key=lambda r: r.get("score", 0.0), reverse=True)[:top_k]
            if not supporting:
                return {
                    "topic": topic,
                    "target_path": None,
                    "confidence": 0.0,
                    "supporting_notes": [],
                    "message": "no memory above threshold - nothing to suggest yet",
                }

            # Tight threshold + minimum 2 matching chunks per target page to
            # avoid picking a page that only mentions the topic once.
            doc_hits = await store.search(topic, kind="doc_chunk", limit=max(top_k, 10), min_score=0.4)
            page_scores: dict[str, list[float]] = {}
            section_of: dict[str, str] = {}
            for d in doc_hits:
                sp = d.get("source_path") or ""
                if not sp:
                    continue
                page_scores.setdefault(sp, []).append(d.get("score", 0.0))
                section_of.setdefault(sp, d.get("source_section") or "")
            # Rank pages by the strongest single chunk match (max), not
            # by sum of all matches. Sum rewards pages that mention the
            # topic in many weak places (e.g. a generic appendix) over a
            # single on-point chunk in the right page. Max picks the
            # page whose most relevant chunk is closest to the topic.
            strict = {p: max(s) for p, s in page_scores.items() if len(s) >= 2}
            relaxed = {p: max(s) for p, s in page_scores.items()}
            candidates = strict or relaxed
            target_path = max(candidates, key=candidates.get) if candidates else None
            target_section = section_of.get(target_path or "", "")
            target_confidence = round(candidates.get(target_path or "", 0.0), 3) if target_path else 0.0

            confidence = min(1.0, sum(m.get("score", 0.0) for m in supporting) / len(supporting))

            supporting_notes = [
                (m.get("content") or "").replace("\n", " ").strip()
                for m in supporting
            ]

            instructions_for_writer = (
                "Do NOT publish the `supporting_notes` verbatim. They are raw "
                "retrieval material captured from prior interactions and may "
                "contain informal shorthand, internal jargon (e.g. 'GOTCHA', "
                "'WORKAROUND'), duplicates, or phrasing specific to a support "
                "channel that does not belong in the published corpus.\n"
                "\n"
                "Step 1 — READ THE SURROUNDING CONTEXT. Before drafting, call "
                "`get_page_section(path=target_path, heading=target_section)` "
                "to fetch the current text of the destination section. Also "
                "consider calling `get_page(path=target_path)` for broader "
                "context when the section is short. The corpus is not assumed "
                "to be any particular genre — it may be a user manual, a "
                "knowledge base, an essay collection, internal runbooks, or "
                "unrelated notes. Infer voice, tense, register, terminology, "
                "and formatting conventions from what is already there. "
                "CRITICAL: the prose MUST be written in the same natural "
                "language detected in the existing section, regardless of "
                "the language of this conversation or of the `topic` string. "
                "If the existing section is in English, write in English; "
                "if it is in Italian, write in Italian; and so on. This is a "
                "hard constraint, not a preference.\n"
                "\n"
                "Step 2 — DRAFT ONE COHERENT PROSE EXTENSION of the section. "
                "It must:\n"
                "  - match the natural language, tone, and structural "
                "    conventions of the existing section (no new heading, no "
                "    bullet list unless the surrounding section itself uses "
                "    them);\n"
                "  - cover only facts that are consistent across the "
                "    supporting notes; drop outliers and near-duplicates;\n"
                "  - omit every piece of internal metadata (stability, "
                "    access_count, ids, scores, timestamps) and every "
                "    support-channel artefact;\n"
                "  - read as if it had always been part of the document — "
                "    no meta-commentary such as 'this section was updated' "
                "    or 'based on user feedback'.\n"
                "\n"
                "Step 3 — SUBMIT. Pass the drafted text to "
                "`propose_doc_patch` as the `prose` argument (with the same "
                "`topic`, `target_path`, `target_section`) to produce the "
                "git commit."
            )

            # Fallback: when no existing page scores high enough, propose
            # creating a new page rather than grafting prose onto a weakly-
            # related target. The caller (LLM) decides whether to accept the
            # proposal or override with an explicit target.
            fallback_proposal = None
            LOW_TARGET_CONFIDENCE = 0.6
            if target_confidence < LOW_TARGET_CONFIDENCE:
                topic_slug = _slug_for_new_file(topic)
                parent_dir = ""
                if target_path:
                    parent_dir = "/".join(target_path.split("/")[:-1])
                suggested_new_path = (
                    f"{parent_dir}/{topic_slug}.md" if parent_dir
                    else f"{topic_slug}.md"
                )
                suggested_title = _title_from_topic(topic)
                fallback_proposal = {
                    "type": "new_page",
                    "suggested_path": suggested_new_path,
                    "suggested_title": suggested_title,
                    "rationale": (
                        f"No existing page covers the topic closely: the "
                        f"strongest chunk match is {target_confidence:.2f} "
                        f"(threshold {LOW_TARGET_CONFIDENCE}). Consider "
                        "creating a new page via "
                        "`propose_doc_patch(create_new_file=True, "
                        "new_path=..., new_title=..., prose=...)` instead "
                        "of grafting prose onto the weak candidate. You "
                        "may override `suggested_path` if the project's "
                        "conventions place this material elsewhere."
                    ),
                }

            # Second guardrail: even when a target page exists, refuse the
            # update if the supporting insights are only loosely related to
            # the topic. Target confidence measures "is there a good place
            # to write?"; insight confidence measures "do we know enough to
            # write it?". Both must pass for a safe PR.
            LOW_INSIGHT_CONFIDENCE = 0.6
            abort_recommendation = None
            if confidence < LOW_INSIGHT_CONFIDENCE:
                abort_recommendation = {
                    "reason": "low_insight_confidence",
                    "overall_confidence": round(confidence, 3),
                    "threshold": LOW_INSIGHT_CONFIDENCE,
                    "message": (
                        f"Supporting insights are weakly correlated with the "
                        f"topic (avg score {confidence:.2f} < "
                        f"{LOW_INSIGHT_CONFIDENCE}). The retrieval returned "
                        "notes that cosine-match the topic but are likely "
                        "off-subject. Do NOT invoke propose_doc_patch "
                        "automatically; either refine the topic wording, "
                        "or capture more on-point insights first via "
                        "`remember()` before retrying."
                    ),
                }

            return {
                "topic": topic,
                "target_path": target_path,
                "target_section": target_section,
                "target_confidence": target_confidence,
                "supporting_notes": supporting_notes,
                "confidence": round(confidence, 3),
                "instructions_for_writer": instructions_for_writer,
                "fallback_proposal": fallback_proposal,
                "abort_recommendation": abort_recommendation,
            }
        except db.MemoryDisabled:
            return {"error": "semantic memory is disabled"}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    async def propose_doc_patch(topic: str, prose: str = "",
                                target_path: str = "",
                                target_section: str = "",
                                create_new_file: bool = False,
                                new_path: str = "",
                                new_title: str = "",
                                top_k: int = 8,
                                dry_run: bool = False,
                                open_pr: bool = False) -> dict:
        """Create a git branch + commit in the docs repo with caller-authored
        prose: either as an extension to an existing page OR as a brand new
        page when no existing target is a good fit.

        Typical flow:
          1. Call `suggest_doc_update(topic=...)` to fetch the target page,
             the matching section, the supporting notes, and — when target
             confidence is low — a `fallback_proposal` suggesting a new page.
          2. You (the caller) write ONE discursive prose block in the voice
             of the existing documentation, following the
             `instructions_for_writer` returned by step 1.
          3a. To extend an existing page:
                propose_doc_patch(topic=..., prose=<your paragraph>)
          3b. To create a new page (fallback_proposal accepted or overridden):
                propose_doc_patch(topic=..., prose=...,
                                  create_new_file=True,
                                  new_path=<rel/path.md>,
                                  new_title=<H1 title>)

        For extension mode, `prose` is inserted verbatim immediately after
        the target H2 section (or at EOF if none). For new-file mode, the
        tool writes `# <new_title>\\n\\n<prose>` to `<content_root>/<new_path>`
        and refuses if the file already exists.

        The tool never adds bullet lists, headings beyond the H1 in new-file
        mode, or internal metadata — what you pass is exactly what gets
        committed.

        If `prose` is empty, the tool returns `status: "needs_prose"` with
        the same material as `suggest_doc_update` so the caller can draft it
        and try again.

        Args:
            topic: Topic identifier used for branch/commit naming.
            prose: Markdown paragraph(s) authored by the caller. Required
                for an actual commit.
            target_path: Override target doc path (relative to content root).
                Ignored when `create_new_file=True`.
            target_section: Override H2 section name to insert after.
                Ignored when `create_new_file=True`.
            create_new_file: When True, write `prose` as a new markdown file
                at `new_path` with `new_title` as the H1. Fails if the file
                already exists.
            new_path: Relative path (under content root) of the new file.
                Required when `create_new_file=True`.
            new_title: H1 title for the new file. Required when
                `create_new_file=True`.
            top_k: How many supporting memories to consider when deriving
                defaults for `target_path` / `target_section` (default 8).
            dry_run: If true, compute the plan but don't touch the repo.
            open_pr: If true, after committing also push the branch and
                open a PR.
        """
        from memory.doc_patch import (
            apply_patch, create_new_file_patch,
            push_branch, open_pull_request, _get_repo_path,
        )

        try:
            top_k = max(1, min(int(top_k), 20))

            # New-file branch: skip target resolution entirely, go straight
            # to create_new_file_patch after validating args.
            if create_new_file:
                if not prose.strip():
                    return {
                        "status": "needs_prose",
                        "topic": topic,
                        "mode": "new_file",
                        "message": (
                            "create_new_file=True requires `prose`. Call "
                            "suggest_doc_update first, accept or override "
                            "its `fallback_proposal`, draft the prose, "
                            "then re-invoke with prose + new_path + new_title."
                        ),
                    }
                if not new_path:
                    return {
                        "status": "error",
                        "topic": topic,
                        "message": "create_new_file=True requires `new_path`",
                    }
                if not new_title:
                    return {
                        "status": "error",
                        "topic": topic,
                        "message": "create_new_file=True requires `new_title`",
                    }
                commit_subject = f"docs: create {new_path} ({topic})"
                commit_body = (
                    f"Topic: {topic}\n"
                    f"New file: {new_path}\n"
                    f"Title: {new_title}\n"
                    f"\n"
                    f"Page authored by the caller via propose_doc_patch."
                )
                result = create_new_file_patch(
                    topic=topic,
                    new_rel_path=new_path,
                    new_title=new_title,
                    prose=prose,
                    commit_message_subject=commit_subject,
                    commit_message_body=commit_body,
                    dry_run=bool(dry_run),
                )
                out = result.to_dict()
                out.update({
                    "topic": topic,
                    "mode": "new_file",
                    "new_path": new_path,
                    "new_title": new_title,
                })
                if open_pr and result.status == "committed" and result.branch:
                    try:
                        repo_path = _get_repo_path()
                        push_branch(repo_path, result.branch)
                        out["pushed"] = True
                        pr_body = (
                            f"New page proposed by MCP.\n\n{commit_body}"
                        )
                        out["pr_url"] = open_pull_request(
                            branch=result.branch,
                            title=commit_subject,
                            body=pr_body,
                        )
                        out["next_steps"] = f"Review the PR at {out['pr_url']}"
                    except Exception as e:
                        log.warning("[memory] open_pr failed: %s", e)
                return out

            # Resolve target via suggest_doc_update's logic when not overridden.
            supporting: list[dict] = []
            if not target_path or not target_section:
                # Only insights here too — see suggest_doc_update for rationale.
                insights = await store.search(topic, kind="insight",
                                              limit=top_k, min_score=0.5)
                supporting = sorted(insights,
                                    key=lambda r: r.get("score", 0.0),
                                    reverse=True)[:top_k]

                doc_hits = await store.search(topic, kind="doc_chunk",
                                              limit=max(top_k, 10), min_score=0.4)
                page_scores: dict[str, list[float]] = {}
                section_of: dict[str, str] = {}
                for d in doc_hits:
                    sp = d.get("source_path") or ""
                    if not sp:
                        continue
                    page_scores.setdefault(sp, []).append(d.get("score", 0.0))
                    section_of.setdefault(sp, d.get("source_section") or "")
                strict = {p: max(s) for p, s in page_scores.items() if len(s) >= 2}
                relaxed = {p: max(s) for p, s in page_scores.items()}
                candidates = strict or relaxed
                resolved_path = max(candidates, key=candidates.get) if candidates else None
                target_path = target_path or (resolved_path or "")
                target_section = target_section or section_of.get(resolved_path or "", "")

            if not prose.strip():
                supporting_notes = [
                    (m.get("content") or "").replace("\n", " ").strip()
                    for m in supporting
                ] if supporting else []
                return {
                    "status": "needs_prose",
                    "topic": topic,
                    "target_path": target_path or None,
                    "target_section": target_section or None,
                    "supporting_notes": supporting_notes,
                    "message": (
                        "No `prose` provided. Call suggest_doc_update first, "
                        "compose a single paragraph in the voice of the target "
                        "documentation page, then re-invoke propose_doc_patch "
                        "with the paragraph as the `prose` argument."
                    ),
                }

            if not target_path:
                return {
                    "status": "skipped",
                    "topic": topic,
                    "message": "no target_path could be inferred — pass one explicitly",
                }

            # Preserve caller prose verbatim. Trim surrounding whitespace and
            # ensure exactly one trailing newline so the diff is clean.
            markdown_block = prose.strip() + "\n"

            commit_subject = f"docs: extend {target_section or target_path} with {topic}"
            commit_body = (
                f"Topic: {topic}\n"
                f"Target: {target_path} (section: {target_section or 'EOF'})\n"
                f"\n"
                f"Paragraph authored by the caller via propose_doc_patch."
            )

            result = apply_patch(
                topic=topic,
                target_rel_path=target_path,
                section_anchor=target_section,
                markdown_block=markdown_block,
                commit_message_subject=commit_subject,
                commit_message_body=commit_body,
                dry_run=bool(dry_run),
            )

            out = result.to_dict()
            out.update({
                "topic": topic,
                "target_path": target_path,
                "target_section": target_section,
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
