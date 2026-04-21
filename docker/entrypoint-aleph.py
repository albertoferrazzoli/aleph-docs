"""Entrypoint for the Aleph container.

Wraps the existing FastAPI app (`aleph.backend.main:app`) with:
- /aleph/api/*  → the API (mounted)
- /aleph/*      → the built frontend (static files)
- /             → redirect to /aleph/login.html

This lets a single uvicorn process serve both routes on ALEPH_PORT,
so the Docker image doesn't need Apache/nginx sidecars.
"""

import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# The Aleph backend uses relative imports (`from . import db, mcp_bridge`)
# so it must be loaded as a proper package. Adding `/app/aleph/` to
# sys.path makes `backend.main` resolvable.
_aleph_root = Path("/app/aleph").resolve()
if str(_aleph_root) not in sys.path:
    sys.path.insert(0, str(_aleph_root))

from backend.main import app as api_app  # existing Aleph FastAPI app

# Also prepare memory package for a shared init_pool. Mounted sub-app
# lifespans are not always invoked by Starlette when wrapped, so we
# init the pool at the root level to guarantee it.
sys.path.insert(0, "/app/mcp")
from memory import db as memory_db  # noqa: E402

FRONTEND_DIST = Path(os.environ.get("ALEPH_FRONTEND_DIST", "/app/aleph/frontend/dist"))
HOST = os.environ.get("ALEPH_HOST", "0.0.0.0")
PORT = int(os.environ.get("ALEPH_PORT", "8765"))


from contextlib import asynccontextmanager
import asyncio


async def _auto_projection_task():
    """Listen for `memory_change` NOTIFY and rebuild the graph snapshot.

    Two coalescing policies fire in whichever triggers first:

    - **Quiet debounce** (`PROJECTION_DEBOUNCE_S`, default 30 s): rebuild
      after that many seconds with no further `memory_change` NOTIFY.
      Ideal for small bursts that settle quickly.

    - **Max staleness** (`PROJECTION_MAX_STALE_S`, default 120 s): rebuild
      at least this often while there are pending changes, even mid-burst.
      Without this, a multi-minute ingest would never refresh the viewer.

    Also fires a coalesced `graph_rebuilt` pg_notify so connected SSE
    clients reload immediately — matches the existing contract in
    aleph/backend/main.py:186.
    """
    import os as _os
    import time as _time
    import psycopg
    from backend import db as aleph_db
    from backend.projection import build_snapshot

    debounce_s = float(_os.environ.get("PROJECTION_DEBOUNCE_S", "30"))
    max_stale_s = float(_os.environ.get("PROJECTION_MAX_STALE_S", "120"))

    dsn = aleph_db.raw_dsn()
    if not dsn:
        print("[auto-projection] PG_DSN not set — auto-rebuild disabled", flush=True)
        return

    # One persistent psycopg connection for LISTEN (not via the pool —
    # LISTEN requires a dedicated connection held for the lifetime).
    try:
        aconn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    except Exception as e:
        print(f"[auto-projection] connect failed: {e}", flush=True)
        return

    try:
        async with aconn.cursor() as cur:
            await cur.execute("LISTEN memory_change")
        print(
            f"[auto-projection] listening on memory_change (debounce={debounce_s}s)",
            flush=True,
        )

        notify_gen = aconn.notifies()
        pending_change = False
        first_pending_at: float | None = None

        async def _do_rebuild():
            try:
                payload = await build_snapshot()
                stats = payload.get("stats") or {}
                n_nodes = int(stats.get("n_nodes") or 0)
                if n_nodes == 0:
                    log.info("[auto-projection] skipping insert — 0 nodes")
                    return
                version = await aleph_db.insert_snapshot(payload)
                # Let connected SSE clients know a new snapshot is ready.
                async with aconn.cursor() as cur2:
                    await cur2.execute(
                        "SELECT pg_notify('graph_rebuilt', %s)",
                        (str(version),),
                    )
                print(
                    f"[auto-projection] snapshot v{version} built "
                    f"nodes={n_nodes} edges={int(stats.get('n_edges') or 0)}",
                    flush=True,
                )
            except Exception as e:
                log.exception("[auto-projection] rebuild failed: %s", e)

        # Loop: drain notifies, choose the smaller of (quiet-debounce,
        # time-until-max-staleness) as the next wait interval.
        while True:
            try:
                if pending_change and first_pending_at is not None:
                    staleness = _time.monotonic() - first_pending_at
                    # Time until we MUST rebuild even if notifies keep arriving.
                    stale_budget = max(0.0, max_stale_s - staleness)
                    timeout = min(debounce_s, stale_budget)
                else:
                    timeout = None  # block indefinitely for the first notify

                fut = asyncio.create_task(notify_gen.__anext__())
                done, _ = await asyncio.wait({fut}, timeout=timeout)
                if fut in done:
                    try:
                        _n = fut.result()
                    except StopAsyncIteration:
                        break
                    if not pending_change:
                        first_pending_at = _time.monotonic()
                    pending_change = True
                    # Did we just cross the staleness ceiling mid-burst?
                    if first_pending_at is not None and (
                        _time.monotonic() - first_pending_at >= max_stale_s
                    ):
                        pending_change = False
                        first_pending_at = None
                        await _do_rebuild()
                else:
                    # Timed out → either debounce settled or staleness hit.
                    fut.cancel()
                    if pending_change:
                        pending_change = False
                        first_pending_at = None
                        await _do_rebuild()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[auto-projection] loop error: {e}", flush=True)
                await asyncio.sleep(5)
    finally:
        try:
            await aconn.close()
        except Exception:
            pass


@asynccontextmanager
async def root_lifespan(_app):
    try:
        await memory_db.init_pool()
    except Exception as e:
        import logging
        logging.getLogger("aleph").warning("[aleph] pool init failed: %s", e)

    # Background auto-projection task — rebuilds the UMAP snapshot after
    # ingest bursts settle. Optional: set AUTO_PROJECTION=false to disable.
    import os as _os
    auto_proj_task = None
    if _os.environ.get("AUTO_PROJECTION", "true").lower() == "true":
        auto_proj_task = asyncio.create_task(_auto_projection_task())

    yield

    if auto_proj_task is not None and not auto_proj_task.done():
        auto_proj_task.cancel()
        try:
            await auto_proj_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await memory_db.close_pool()
    except Exception:
        pass


root = FastAPI(
    title="aleph-docs (docker)",
    description="Root wrapper: serves the Aleph frontend and proxies /aleph/api to the backend.",
    lifespan=root_lifespan,
)


@root.get("/")
async def redirect_to_login():
    return RedirectResponse(url="/aleph/login.html")


@root.get("/healthz")
async def healthz():
    return {"status": "ok"}


# Mount the API under /aleph/api. The inner app defines routes like
# @app.get('/health'), @app.get('/graph'), etc. — they become
# /aleph/api/health, /aleph/api/graph, matching what the frontend expects
# (frontend's Vite `base: '/aleph/'` + fetch('/aleph/api/...')).
root.mount("/aleph/api", api_app)

# Mount the static frontend under /aleph. `html=True` serves index.html
# for directory requests. login.html / assets/ / favicon are siblings of
# index.html in the build output.
if FRONTEND_DIST.is_dir():
    root.mount(
        "/aleph",
        StaticFiles(directory=str(FRONTEND_DIST), html=True),
        name="frontend",
    )
else:
    # Non-fatal: the API still works even if the frontend wasn't bundled.
    import logging
    logging.getLogger("aleph").warning(
        "[aleph] frontend dist not found at %s — only /aleph/api/* will work",
        FRONTEND_DIST,
    )


if __name__ == "__main__":
    uvicorn.run(root, host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="*")
