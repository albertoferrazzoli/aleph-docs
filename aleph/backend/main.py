"""Aleph FastAPI app — 3D semantic memory viewer backend.

Endpoints (mounted at `/aleph/api` in production via uvicorn --root-path):
    GET  /health
    GET  /graph
    GET  /graph/stream        (SSE)
    POST /search
    GET  /node/{id}
    POST /remember            (write; X-Aleph-Key)
    POST /forget/{id}         (write; X-Aleph-Key)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import db, mcp_bridge
from .auth import require_api_key

log = logging.getLogger("aleph")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await db.init_pool()
    except Exception as e:
        log.warning("[aleph] init_pool failed at startup: %s", e)
    yield
    try:
        await db.close_pool()
    except Exception as e:  # pragma: no cover
        log.warning("[aleph] close_pool failed: %s", e)


app = FastAPI(title="Aleph backend", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SearchBody(BaseModel):
    query: str = Field(..., min_length=1)
    kind: Optional[str] = None
    limit: int = Field(10, ge=1, le=100)
    min_score: float = Field(0.15, ge=0.0, le=1.0)


class RememberBody(BaseModel):
    content: str = Field(..., min_length=1)
    context: Optional[str] = ""
    source_path: Optional[str] = None
    tags: Optional[list[str]] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    memory_count: Optional[int] = None
    snapshot_version: Optional[int] = None
    graph_nodes: int = 0
    status = "ok"
    error: Optional[str] = None

    try:
        memory_count = await db.count_memories()
        snapshot_version = await db.get_latest_version()
        if snapshot_version is not None:
            snap = await db.get_latest_snapshot()
            if snap and isinstance(snap.get("payload"), dict):
                nodes = snap["payload"].get("nodes") or []
                graph_nodes = len(nodes)
        if memory_count is None:
            status = "degraded"
            error = "memory subsystem disabled"
    except Exception as e:
        log.warning("[aleph] /health db error: %s", e)
        status = "degraded"
        error = str(e)

    body = {
        "status": status,
        "memory_count": memory_count,
        "snapshot_version": snapshot_version,
        "graph_nodes": graph_nodes,
    }
    if error:
        body["error"] = error
    return JSONResponse(body, status_code=200)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

@app.get("/graph")
async def get_graph(version: Optional[int] = Query(default=None)) -> JSONResponse:
    try:
        snap = await db.get_latest_snapshot()
    except Exception as e:
        log.warning("[aleph] /graph error: %s", e)
        return JSONResponse(
            {"nodes": [], "edges": [], "version": 0, "total_nodes": 0,
             "error": str(e)},
            status_code=200,
        )

    if snap is None:
        return JSONResponse(
            {"nodes": [], "edges": [], "version": 0, "total_nodes": 0},
            status_code=200,
        )

    current = snap["version"]
    if version is not None and int(version) == current:
        return JSONResponse({"version": current, "unchanged": True})

    payload = snap["payload"] or {}
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []

    # Include memories inserted AFTER this snapshot was built — otherwise they
    # disappear on page reload until the next projection run (up to 1h).
    # Each pending memory carries `anchor_ids` (top-3 nearest neighbors among
    # the snapshot nodes) so the client can anchor its 3D position.
    pending: list = []
    try:
        known_ids = {str(n.get("id")) for n in nodes if n.get("id")}
        pending = await db.fetch_pending_memories(known_ids)
    except Exception as e:
        log.warning("[aleph] /graph pending enrichment failed: %s", e)

    return JSONResponse({
        "nodes": nodes,
        "edges": edges,
        "version": current,
        "total_nodes": len(nodes) + len(pending),
        "pending": pending,
    })


# ---------------------------------------------------------------------------
# Graph SSE stream
# ---------------------------------------------------------------------------

@app.get("/graph/stream")
async def graph_stream(request: Request):
    async def event_gen():
        import psycopg

        dsn = db.raw_dsn()
        if not dsn:
            yield {"event": "error", "data": json.dumps({"error": "PG_DSN not set"})}
            return

        try:
            aconn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        except Exception as e:
            log.warning("[aleph] /graph/stream connect failed: %s", e)
            yield {"event": "error", "data": json.dumps({"error": str(e)})}
            return

        try:
            async with aconn.cursor() as cur:
                await cur.execute("LISTEN memory_change")
                await cur.execute("LISTEN graph_rebuilt")

            last_ping = asyncio.get_event_loop().time()
            notify_gen = aconn.notifies()
            notify_task: asyncio.Task | None = None

            while True:
                if await request.is_disconnected():
                    break

                if notify_task is None:
                    notify_task = asyncio.create_task(notify_gen.__anext__())

                done, _pending = await asyncio.wait(
                    {notify_task}, timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                now = asyncio.get_event_loop().time()
                if now - last_ping >= 15.0:
                    yield {"event": "ping", "data": "{}"}
                    last_ping = now

                if notify_task in done:
                    try:
                        n = notify_task.result()
                    except StopAsyncIteration:
                        break
                    except Exception as e:  # pragma: no cover
                        log.warning("[aleph] notify loop error: %s", e)
                        break
                    notify_task = None

                    channel = getattr(n, "channel", "memory_change")
                    payload = getattr(n, "payload", "") or "{}"
                    if channel == "graph_rebuilt":
                        yield {
                            "event": "version_bump",
                            "data": json.dumps({"version": int(payload or 0)}),
                        }
                    else:
                        yield {"event": "memory_change", "data": payload}
        finally:
            try:
                await aconn.close()
            except Exception:  # pragma: no cover
                pass

    return EventSourceResponse(event_gen())


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.post("/search")
async def search(body: SearchBody) -> JSONResponse:
    try:
        results = await mcp_bridge.search(
            query=body.query, kind=body.kind,
            limit=body.limit, min_score=body.min_score,
        )
        return JSONResponse({"results": results, "count": len(results)})
    except db.MemoryDisabled as e:
        return JSONResponse(
            {"results": [], "count": 0, "error": f"memory disabled: {e}"},
            status_code=200,
        )
    except Exception as e:
        log.warning("[aleph] /search error: %s", e)
        return JSONResponse(
            {"results": [], "count": 0, "error": str(e)},
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Node detail
# ---------------------------------------------------------------------------

@app.get("/node/{node_id}")
async def get_node(node_id: str) -> JSONResponse:
    try:
        detail = await mcp_bridge.node_detail(node_id)
    except Exception as e:
        log.warning("[aleph] /node/%s error: %s", node_id, e)
        return JSONResponse({"error": str(e)}, status_code=200)
    if detail is None:
        raise HTTPException(status_code=404, detail="node not found")
    return JSONResponse(detail)


@app.get("/node/{node_id}/audit")
async def get_node_audit(node_id: str, limit: int = 20) -> JSONResponse:
    """Return the audit trail for a specific memory id."""
    try:
        limit = max(1, min(int(limit), 200))
        events = await db.get_node_audit(node_id, limit=limit)
        return JSONResponse({"node_id": node_id, "count": len(events), "events": events})
    except Exception as e:
        log.warning("[aleph] /node/%s/audit error: %s", node_id, e)
        return JSONResponse({"error": str(e), "events": []}, status_code=200)


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------

@app.post("/remember", dependencies=[Depends(require_api_key)])
async def remember(body: RememberBody) -> JSONResponse:
    try:
        out = await mcp_bridge.remember(
            content=body.content, context=body.context or "",
            source_path=body.source_path, tags=body.tags or [],
        )
        return JSONResponse(out)
    except Exception as e:
        log.warning("[aleph] /remember error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=200)


@app.post("/forget/{node_id}", dependencies=[Depends(require_api_key)])
async def forget(node_id: str) -> JSONResponse:
    try:
        out = await mcp_bridge.forget(node_id)
        return JSONResponse(out)
    except Exception as e:
        log.warning("[aleph] /forget error: %s", e)
        return JSONResponse({"error": str(e), "deleted": False}, status_code=200)
