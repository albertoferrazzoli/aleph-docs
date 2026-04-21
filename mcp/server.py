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
        f"MCP server exposing Markdown documentation indexed from {DOCS_REPO} "
        "plus a long-term semantic memory (pgvector). Every answer should be "
        "grounded in the docs and/or the accumulated insights.\n\n"
        "KNOWLEDGE SOURCES:\n"
        "- SQLite FTS5 lexical index over the repo's `content/` markdown\n"
        "- Postgres + pgvector memory with three kinds:\n"
        "    doc_chunk   — same docs re-embedded for semantic search\n"
        "    interaction — past queries (auto-recorded)\n"
        "    insight     — notes saved explicitly via `remember()`\n\n"
        f"DETECTED SECTIONS (from DOCS_SECTIONS env): {', '.join(DOCS_SECTIONS)}\n\n"
        "RECOMMENDED WORKFLOW for support / Q&A:\n"
        "1. For natural-language questions → `semantic_search(query)` so that\n"
        "   insights + interactions are consulted together with the docs.\n"
        "2. For exact-keyword lookups → `search_docs(query)` (BM25, faster).\n"
        "3. Read the full content via `get_page(path)` or\n"
        "   `get_page_section(path, heading)` before explaining.\n"
        "4. For code examples → `search_code_examples` or `get_code_blocks`.\n"
        "5. Narrow helpers: `find_command_line_option`, `find_error_message`.\n"
        "6. When you learn something non-obvious → `remember(content, context)`.\n"
        "7. After repeated hits on the same insight → `propose_doc_patch` to\n"
        "   push canonical docs.\n"
        "8. Always cite the page path when quoting the documentation.\n\n"
        "The documentation is re-indexed hourly from GitHub."
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
