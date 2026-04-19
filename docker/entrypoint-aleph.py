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


@asynccontextmanager
async def root_lifespan(_app):
    try:
        await memory_db.init_pool()
    except Exception as e:
        import logging
        logging.getLogger("aleph").warning("[aleph] pool init failed: %s", e)
    yield
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
