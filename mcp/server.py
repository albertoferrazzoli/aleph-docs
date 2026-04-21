"""MCP server exposing Markdown documentation + a long-term semantic memory.

See README.md at the repo root for the full design. This file wires the
FastMCP app, registers tool modules and (optionally) starts a combined
Starlette HTTP app with /health + /mcp (streamable HTTP) + /sse.
"""

import os

from dotenv import load_dotenv

load_dotenv()

from fastmcp import FastMCP

from auth import APIKeyMiddleware
from tools import content, lookup, meta, navigation, search, memory as memory_tools
from memory import db as memory_db


SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "AlephDocs")
DOCS_REPO = os.environ.get("DOCS_REPO_URL", "<your docs repo>")
# Space-separated list of top-level content/ subdirs used by the product.
# Example: "guides reference api". Used only in the human-readable instructions.
DOCS_SECTIONS = os.environ.get("DOCS_SECTIONS", "guides reference api").split()


mcp = FastMCP(
    name=SERVER_NAME,
    instructions=(
        f"MCP server exposing a UNIFIED multimodal knowledge base over {DOCS_REPO}. "
        "Do NOT treat the markdown documentation and the pgvector memory as two "
        "separate corpora: they live in one single, searchable space and every "
        "user question should be answered by querying ALL of it at once.\n\n"

        "THE KNOWLEDGE BASE CONTAINS (all in one pgvector table):\n"
        "  doc_chunk        — text from markdown documentation files\n"
        "  pdf_page         — rendered PDF pages (image-embedded, hybrid mode)\n"
        "  pdf_text         — extracted PDF text per page (text-embedded)\n"
        "  image            — standalone images + images embedded in PDFs\n"
        "  video_scene      — video scene segments (video-embedded, hybrid mode)\n"
        "  video_transcript — Whisper transcripts of each scene (text-embedded)\n"
        "  audio_clip       — audio windows (audio-embedded, hybrid mode)\n"
        "  audio_transcript — Whisper transcripts of audio (text-embedded)\n"
        "  insight          — notes saved explicitly via `remember()`\n"
        "  interaction      — past user queries (auto-recorded)\n\n"

        "Depending on the deployment one or more of the above may be empty "
        "(e.g. a text-only setup has no video_scene but still has video_transcript). "
        "Check `memory_stats()` if you're unsure what's available.\n\n"

        "WORKFLOW FOR USER QUESTIONS — default shape:\n"
        "1. Call `semantic_search(query)` WITHOUT a `kind` filter so every\n"
        "   modality (markdown, PDF text, video transcripts, audio transcripts,\n"
        "   insights) contributes to the answer. The system re-ranks by\n"
        "   similarity × forgetting-curve decay, so relevance wins over source.\n"
        "2. Write a single synthesised answer that cites each hit by its\n"
        "   `source_path` and, when present, a human label of the source kind\n"
        "   (\"from the manual\", \"from the video at 0:42\", \"from the PDF\")\n"
        "   — but present it as ONE answer, not a per-source report.\n"
        "3. Do NOT say 'the docs are empty' when only markdown is empty but\n"
        "   video_transcripts / pdf_text are present. The corpus is the UNION.\n"
        "4. For visual questions ('show me a chart of X') → `search_images`.\n"
        "5. For exact-keyword lookups → `search_docs` (BM25 over md only).\n"
        "6. When you learn something worth saving → `remember(content, context)`.\n"
        "7. When user patterns reveal doc gaps → `propose_doc_patch`.\n"
        "8. Always cite: `source_path` for text/PDF, timestamp for video/audio.\n\n"

        f"DETECTED SECTIONS (from DOCS_SECTIONS env): {', '.join(DOCS_SECTIONS)}\n\n"
        "The knowledge base is kept in sync incrementally by the reconciler; "
        "user-added content (files dropped in docs/) becomes searchable within seconds."
    ),
)

for module in [search, navigation, content, lookup, meta, memory_tools]:
    module.register(mcp)


if __name__ == "__main__":
    import asyncio
    from contextlib import asynccontextmanager

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse

    from indexer import ensure_repo, incremental_update, open_db

    # On startup: make sure the repo and index are present.
    try:
        ensure_repo()
        conn = open_db()
        try:
            a, u, r = incremental_update(conn)
            print(f"[startup] docs index update: +{a} / ~{u} / -{r}")
        finally:
            conn.close()
    except Exception as e:
        print(f"[startup] docs indexer warning: {e}")

    async def health(_req):
        try:
            from indexer import open_db as _open
            conn = _open()
            n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            conn.close()
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

        payload = {"status": "ok", "pages": n}
        try:
            hc = await memory_db.health_check()
            payload["memory_enabled"] = bool(hc.get("enabled"))
            payload["memory_count"] = hc.get("memory_count")
            if hc.get("error"):
                payload["memory_error"] = hc["error"]
        except Exception as e:
            payload["memory_enabled"] = False
            payload["memory_count"] = None
            payload["memory_error"] = str(e)

        # Ingest task progress (media reconciler). Always present so
        # operators can tell the difference between "no task ever ran"
        # and "ran and finished".
        try:
            from memory.ingest_task import get_ingest_task
            payload["ingest"] = get_ingest_task().snapshot()
        except Exception as e:
            payload["ingest"] = {"state": "unavailable", "error": str(e)}
        return JSONResponse(payload)

    streamable_app = mcp.http_app(transport="streamable-http", path="/mcp")
    sse_app = mcp.http_app(transport="sse", path="/sse")

    @asynccontextmanager
    async def combined_lifespan(_app):
        try:
            await memory_db.init_pool()
        except Exception as e:
            print(f"[memory] pool init failed (continuing without memory): {e}")

        # Media reconciler: background task + (optional) filesystem watcher.
        # In DOCS_MODE=git the initial run happens here (piggybacks on the
        # pull done above), the watcher is skipped.
        ingest_bg_task = None
        watcher = None
        try:
            import indexer as _indexer
            from memory.ingest_task import get_ingest_task
            from memory.watcher import start_if_local
            ingest_on_boot = os.environ.get(
                "INGEST_MEDIA_ON_BOOT", "true"
            ).lower() == "true"
            it = get_ingest_task()

            async def _bg():
                try:
                    summary = await it.run_once()
                    print(
                        f"[ingest] initial media reconcile done: "
                        f"+{summary.added} ~{summary.updated} "
                        f"-{summary.removed} skip={summary.skipped} "
                        f"errors={len(summary.errors)} "
                        f"in {round(summary.finished_at - summary.started_at, 1)}s"
                    )
                except Exception as e:
                    print(f"[ingest] initial reconcile failed: {e}")

            if ingest_on_boot:
                ingest_bg_task = asyncio.create_task(_bg())
            else:
                print("[ingest] INGEST_MEDIA_ON_BOOT=false — skipping boot scan")

            loop = asyncio.get_running_loop()
            watcher = start_if_local(
                _indexer.REPO_PATH
                if _indexer.DOCS_MODE == "local"
                else (_indexer.REPO_PATH / _indexer.CONTENT_SUBDIR
                      if _indexer.CONTENT_SUBDIR
                      else _indexer.REPO_PATH),
                it, loop,
            )
        except Exception as e:
            print(f"[ingest] setup failed (continuing without reconciler): {e}")

        try:
            async with streamable_app.lifespan(streamable_app):
                async with sse_app.lifespan(sse_app):
                    yield {}
        finally:
            if watcher is not None:
                try:
                    watcher.stop()
                except Exception as e:
                    print(f"[watcher] stop warning: {e}")
            if ingest_bg_task is not None:
                try:
                    if not ingest_bg_task.done():
                        ingest_bg_task.cancel()
                except Exception:
                    pass
            try:
                await memory_db.close_pool()
            except Exception as e:
                print(f"[memory] pool close warning: {e}")

    routes = [Route("/health", health)] + list(streamable_app.routes) + list(sse_app.routes)
    combined = Starlette(routes=routes, lifespan=combined_lifespan)
    app = APIKeyMiddleware(combined)

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8001"))
    print(f"{SERVER_NAME} MCP starting on {host}:{port}")
    print(f"  Streamable HTTP: http://{host}:{port}/mcp")
    print(f"  SSE:             http://{host}:{port}/sse")
    print(f"  Health:          http://{host}:{port}/health")
    uvicorn.run(app, host=host, port=port)
