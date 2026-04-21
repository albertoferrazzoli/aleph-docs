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

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
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
    # Activate the persisted workspace BEFORE the pool opens so env
    # vars (PG_DSN dbname) are in place. activate() handles pool init
    # internally. Falls back to a plain init_pool when no workspaces
    # are configured.
    try:
        from memory import workspace_manager as _wm  # type: ignore
        ws = _wm.resolve_initial()
        if ws is not None:
            await _wm.activate(ws)
            log.info("[aleph/workspaces] active=%s docs=%s pg_db=%s",
                     ws.name, ws.docs_path, ws.pg_db)
        else:
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
# Media streaming
# ---------------------------------------------------------------------------

# MEDIA_ROOT is the allowlist base for on-disk media references. Anything
# outside this tree is refused unless it lives under /tmp/ (dev-only, for
# files explicitly added via remember_media with an absolute local path).
# Keeping /tmp as a second allowed prefix matches the dev workflow
# documented in the PRD — production deploys should put their media under
# MEDIA_ROOT and leave /tmp cold.
_MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/opt/aleph-docs/docs")).resolve()


def _resolve_media_path(media_ref: str) -> Path | None:
    """Validate `media_ref` and return a filesystem Path safe to serve.

    Rules (all must pass):
      1. Split on '#' — the part before the fragment is the on-disk path.
      2. Must be absolute after resolve() (symlinks followed).
      3. Must be a regular existing file.
      4. Resolved path must live under MEDIA_ROOT **or** start with '/tmp/'.
         Symlinks whose target is under MEDIA_ROOT are accepted because
         .resolve() follows them before the allowlist check.

    Returns None on any failure — caller maps to 403/404 as appropriate.
    """
    if not media_ref:
        return None
    # Strip fragment (#page=N, #t=1.2, #t=1.2,3.4 …).
    raw = media_ref.split("#", 1)[0]
    if not raw:
        return None
    try:
        p = Path(raw)
    except Exception:
        return None
    if not p.is_absolute():
        return None
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError):
        return None
    if not resolved.is_file():
        return None
    # Allowlist check.
    try:
        resolved.relative_to(_MEDIA_ROOT)
        return resolved
    except ValueError:
        pass
    # Dev/escape hatch: /tmp/* is allowed for ad-hoc remember_media calls.
    try:
        resolved.relative_to(Path("/tmp").resolve())
        return resolved
    except ValueError:
        return None


@app.get("/preview/{memory_id}")
async def get_preview(memory_id: str) -> "Response":
    """Return the stored preview thumbnail (JPEG) for a memory.

    Fast lightweight endpoint used by external tools (e.g. Claude Desktop
    markdown links). Unlike /media it never hits the source file — just
    decodes `preview_b64` from the row. 404 if no preview stored.
    """
    import base64 as _b64
    from starlette.responses import Response
    if not db.is_enabled():
        raise HTTPException(status_code=503, detail="memory disabled")
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT preview_b64 FROM memories WHERE id = %s",
                (memory_id,),
            )
            row = await cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="no preview")
    try:
        data = _b64.b64decode(row[0])
    except Exception:
        raise HTTPException(status_code=500, detail="preview decode error")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/media/{memory_id}")
async def get_media(memory_id: str, request: Request) -> FileResponse:
    """Stream raw media bytes referenced by a memory row.

    Auth: relies on Apache-level Basic Auth in production; no extra
    X-Aleph-Key is required (media is read-only and the viewer needs to
    fetch it from an <img>/<video> tag that can't set custom headers).

    Path safety: the on-disk path must resolve under MEDIA_ROOT (env,
    defaults to /opt/aleph-docs/docs) or under /tmp/. Traversal attempts
    (`../..`, symlinks escaping the allowlist, non-absolute paths) yield
    404/403.

    For PDFs referenced as `file.pdf#page=3` the whole file is returned;
    the client uses the fragment to open the viewer at the requested
    page.
    """
    if not db.is_enabled():
        raise HTTPException(status_code=503, detail="memory subsystem disabled")

    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT media_ref, media_type, preview_b64, metadata "
                    "FROM memories WHERE id = %s",
                    (memory_id,),
                )
                row = await cur.fetchone()
    except Exception as e:
        log.warning("[aleph] /media/%s db error: %s", memory_id, e)
        raise HTTPException(status_code=500, detail="db error")

    if not row:
        raise HTTPException(status_code=404, detail="memory not found")
    media_ref, media_type, preview_b64, metadata = row

    # Image-kind nodes whose media_ref points to a non-image source
    # (e.g. video keyframes with media_ref=<file>.mp4#t=N, or PDF pages
    # routed as kind=image on backends lacking the 'pdf' modality).
    # The underlying raster lives in a tempdir that is wiped after
    # ingest, so serve the persisted thumbnail from preview_b64
    # (~16 KB per frame — small, already optimised at ingest time).
    _virtual_origins = {"video_keyframe", "pdf_page"}
    if (
        (media_type or "").startswith("image/")
        and isinstance(metadata, dict)
        and metadata.get("origin") in _virtual_origins
        and preview_b64
    ):
        import base64 as _b64
        from starlette.responses import Response
        try:
            png_bytes = _b64.b64decode(preview_b64)
        except Exception:
            raise HTTPException(status_code=500, detail="bad preview payload")
        return Response(content=png_bytes, media_type="image/jpeg")
    if not media_ref:
        raise HTTPException(status_code=404, detail="no media_ref on this memory")

    resolved = _resolve_media_path(media_ref)
    if resolved is None:
        log.warning(
            "[aleph] /media/%s refused: ref=%r not under MEDIA_ROOT=%s or /tmp",
            memory_id, media_ref, _MEDIA_ROOT,
        )
        raise HTTPException(status_code=403, detail="media path not allowed")

    # Special case: PDF-extracted image nodes have media_ref like
    # `/foo.pdf#page=3&img=2` but media_type='image/png'. Re-extract the
    # embedded raster on demand so we don't need persistent storage.
    # IMPORTANT: pypdfium2 PdfObject holds refs into the parent page;
    # closing the page before extracting the bitmap segfaults. Extract
    # INSIDE the open-page block.
    import re as _re
    if (media_type or "").startswith("image/") and "#page=" in media_ref and "img=" in media_ref:
        m = _re.search(r"#page=(\d+)(?:&img=(\d+))?", media_ref)
        if m and m.group(2):
            page_n = int(m.group(1))
            img_n = int(m.group(2))
            import io as _io
            png_bytes: bytes | None = None
            try:
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(str(resolved))
                try:
                    page = pdf[page_n - 1]
                    try:
                        imgs = list(page.get_objects(
                            filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,),
                            max_depth=5,
                        ))
                        if img_n < 1 or img_n > len(imgs):
                            raise HTTPException(status_code=404, detail="image index out of range")
                        # Extract WHILE page is still open.
                        pil = imgs[img_n - 1].get_bitmap().to_pil()
                        buf = _io.BytesIO()
                        pil.save(buf, format="PNG", optimize=True)
                        png_bytes = buf.getvalue()
                    finally:
                        page.close()
                finally:
                    pdf.close()
            except HTTPException:
                raise
            except Exception as e:
                log.warning("[aleph] /media/%s pdf image extract failed: %s", memory_id, e)
                raise HTTPException(status_code=500, detail="pdf image extract error")
            if png_bytes is not None:
                from starlette.responses import Response
                return Response(content=png_bytes, media_type="image/png")

    mt = media_type or "application/octet-stream"
    return FileResponse(str(resolved), media_type=mt, filename=resolved.name)


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

class SwitchWorkspaceBody(BaseModel):
    name: str = Field(..., min_length=1)
    reindex: bool = True


@app.get("/workspaces")
async def get_workspaces() -> JSONResponse:
    """List configured workspaces and mark the active one."""
    try:
        from memory.workspaces import load_workspaces  # type: ignore
        from memory.workspace_state import read_active  # type: ignore
        workspaces = [w.to_dict() for w in load_workspaces()]
        active = read_active() or (workspaces[0]["name"] if workspaces else None)
        return JSONResponse({"active": active, "workspaces": workspaces})
    except Exception as e:
        log.warning("[aleph] /workspaces failed: %s", e)
        raise HTTPException(500, f"workspaces listing failed: {e}")


@app.post("/workspaces/active")
async def set_active_workspace(body: SwitchWorkspaceBody) -> JSONResponse:
    """Switch the active workspace in-process + persist the choice.

    NOT gated by X-Aleph-Key: switching is a view-layer action (it
    swaps which DB is active, not the data itself), and basic-auth on
    the perimeter already authenticates the user. Gating it as a
    "write" endpoint made the viewer logout users without a configured
    write key every time they changed workspace.

    Rewrites env, swaps the pool against the target DB, resets the
    embedder cache. The state file is the source of truth for both
    the aleph backend and the mcp — the mcp watches it and
    re-activates automatically within a few seconds.
    """
    try:
        from memory import workspace_manager as _wm  # type: ignore
        from memory.workspaces import get_by_name, load_workspaces  # type: ignore
        ws = get_by_name(body.name)
        if ws is None:
            return JSONResponse(
                {
                    "error": f"unknown workspace {body.name!r}",
                    "available": [w.name for w in load_workspaces()],
                },
                status_code=404,
            )
        summary = await _wm.activate(ws)
        return JSONResponse(summary)
    except Exception as e:
        log.warning("[aleph] /workspaces/active failed: %s", e)
        raise HTTPException(500, f"workspace switch failed: {e}")


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
