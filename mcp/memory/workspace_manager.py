"""Runtime workspace switching.

Given a :class:`Workspace` config, this module:

    1. Rewrites the process-level env vars the rest of the codebase
       reads (PG_DSN, EMBED_BACKEND, EMBED_DIM, HYBRID_MEDIA_EMBEDDING,
       LOCAL_DOCS_PATH, LOCAL_EMBED_DIM).
    2. Ensures the target Postgres database exists and has the schema
       loaded with the correct `vector(N)` dim.
    3. Closes the current pool and re-opens it against the new DSN.
    4. Resets the embedder backend cache so the new EMBED_BACKEND takes
       effect on the next embed() call.
    5. Persists the active workspace name on disk.

The reconciler is NOT driven from here — the caller (the MCP tool) is
expected to kick one off after the switch returns, because that step
is long-running and should surface progress through the existing
ingest_task machinery.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from . import db, embedders, workspace_state
from .workspaces import Workspace, get_by_name

log = logging.getLogger("memory.workspaces")


def _rewrite_dsn_dbname(dsn: str, new_db: str) -> str:
    """Return a DSN that points at `new_db` on the same host/port/creds."""
    # libpq key=value form ("host=... dbname=... user=...")
    if "=" in dsn and "://" not in dsn:
        parts = shlex.split(dsn)
        kv = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k] = v
        kv["dbname"] = new_db
        return " ".join(f"{k}={v}" for k, v in kv.items())
    # URL form (postgresql://user:pass@host:port/db?...)
    u = urlparse(dsn)
    new_path = "/" + new_db.lstrip("/")
    return urlunparse(u._replace(path=new_path))


async def _ensure_database(dsn: str, db_name: str) -> bool:
    """Create `db_name` if it doesn't exist on the same server. Returns
    True when the DB was newly created, False when it already existed.
    """
    # Connect to the maintenance DB `postgres` on the same server with
    # the same creds. psycopg is async but CREATE DATABASE can't run
    # inside a transaction — use autocommit mode.
    import psycopg  # type: ignore

    maintenance_dsn = _rewrite_dsn_dbname(dsn, "postgres")
    created = False
    async with await psycopg.AsyncConnection.connect(
        maintenance_dsn, autocommit=True,
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,),
            )
            if await cur.fetchone() is None:
                # Identifier quoting: Postgres doesn't support %s for DB
                # names. Validate strictly — alnum + underscore only.
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", db_name):
                    raise ValueError(
                        f"_ensure_database: invalid pg_db name {db_name!r}"
                    )
                await cur.execute(f'CREATE DATABASE "{db_name}"')
                created = True
                log.info("[workspaces] created database %s", db_name)
    return created


async def _apply_schema(dsn: str, dim: int) -> None:
    """Run the memory schema SQL against `dsn`, substituting vector(N)
    with the workspace's embed dim. Idempotent.
    """
    import psycopg  # type: ignore

    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text(encoding="utf-8")
    # Same one-line sed the init-memory.sh wrapper does in the db init
    # container — rewrites vector(1536) → vector(<dim>).
    sql = re.sub(r"vector\(1536\)", f"vector({int(dim)})", sql)

    # Also apply aleph additions (graph_snapshot) + triggers so the
    # viewer works immediately against the new DB. Resolve paths
    # relative to the repo; fall back silently if not found (tests).
    extras: list[Path] = []
    for candidate in (
        Path(__file__).parents[2] / "aleph" / "backend" / "schema_additions.sql",
        Path(__file__).parents[2] / "aleph" / "backend" / "triggers.sql",
        Path("/app/aleph/backend/schema_additions.sql"),
        Path("/app/aleph/backend/triggers.sql"),
    ):
        if candidate.is_file() and candidate not in extras:
            extras.append(candidate)

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        # Schema uses multiple ALTER TYPE ADD VALUE which cannot live
        # in one transaction; run it as autocommit to mirror psql -f.
        await conn.set_autocommit(True)
        async with conn.cursor() as cur:
            await cur.execute(sql)
            for p in extras:
                try:
                    await cur.execute(p.read_text(encoding="utf-8"))
                except Exception as e:
                    log.warning("[workspaces] extra %s failed (non-fatal): %s", p, e)


def _apply_env(ws: Workspace) -> None:
    """Push the workspace into the process env so everything that reads
    os.environ on the hot path picks it up (embedders, chunkers, ASR,
    indexer-resolution).
    """
    # PG_DSN: preserve host/creds from the *currently configured* DSN
    # and swap only the dbname if the workspace overrides pg_db.
    current_dsn = os.environ.get("PG_DSN", "").strip()
    if ws.pg_db and current_dsn:
        os.environ["PG_DSN"] = _rewrite_dsn_dbname(current_dsn, ws.pg_db)
    os.environ["EMBED_BACKEND"] = ws.backend
    os.environ["EMBED_DIM"] = str(ws.dim)
    os.environ["HYBRID_MEDIA_EMBEDDING"] = "true" if ws.hybrid else "false"
    os.environ["LOCAL_DOCS_PATH"] = ws.docs_path
    if ws.local_embed_dim is not None:
        os.environ["LOCAL_EMBED_DIM"] = str(ws.local_embed_dim)


async def activate(ws: Workspace) -> dict:
    """Apply a workspace switch in-process. Returns a small summary dict.

    Raises:
        ValueError on malformed workspace (e.g. invalid pg_db name).
        psycopg / connection errors on Postgres issues.
    """
    log.info("[workspaces] activating %r (backend=%s dim=%d docs=%s)",
             ws.name, ws.backend, ws.dim, ws.docs_path)

    # 1) env swap — must happen BEFORE any pool / backend init reads env
    _apply_env(ws)

    new_dsn = os.environ["PG_DSN"]

    # 2) DB provisioning (skip when pg_db not specified — use current DB)
    db_created = False
    if ws.pg_db:
        db_created = await _ensure_database(new_dsn, ws.pg_db)
        if db_created:
            # New DB → apply full schema + aleph extras (triggers etc).
            await _apply_schema(new_dsn, ws.dim)
        # Existing DBs: DO NOT re-apply. Idempotent schemas sound safe
        # in isolation but when two processes (aleph + mcp) activate
        # the same workspace concurrently their parallel
        # `DROP TRIGGER ... CREATE TRIGGER ...` statements deadlock
        # on the memories table. The schema files are authored as
        # first-boot DDL, not hot paths.

    # 3) pool restart
    await db.close_pool()
    await db.init_pool()

    # 4) embedder cache reset — so the next get_backend() reads the new
    # EMBED_BACKEND env var instead of the cached instance.
    embedders._reset_cache_for_tests()

    # 4b) indexer path refresh + markdown rebuild — the `indexer`
    # module captures REPO_PATH / LOCAL_DOCS_PATH at import time, so
    # without this step the markdown indexer would keep scanning the
    # previous workspace's tree and cross-pollute doc_chunks. We also
    # drop and re-populate the SQLite FTS5 pages table so lexical
    # lookups track the new workspace's scope, and re-embed the new
    # doc_chunks into the workspace's DB.
    try:
        import indexer as _indexer  # type: ignore
        if hasattr(_indexer, "refresh_paths"):
            _indexer.refresh_paths()
        if hasattr(_indexer, "open_db") and hasattr(_indexer, "iter_doc_files"):
            # Re-implement `indexer.rebuild()` in async-safe form:
            # its stock version calls `asyncio.run(_flush_memory(...))`
            # which blows up inside our running loop.
            sconn = _indexer.open_db()
            try:
                _indexer._pending_embeds.clear()
                _indexer._pending_images.clear()
                sconn.executescript(
                    "DELETE FROM pages; DELETE FROM pages_fts; "
                    "DELETE FROM code_blocks; DELETE FROM code_blocks_fts;"
                )
                n = 0
                for abs_path in _indexer.iter_doc_files(_indexer.REPO_PATH):
                    rel = abs_path.relative_to(
                        _indexer.REPO_PATH / _indexer.CONTENT_SUBDIR
                        if _indexer.CONTENT_SUBDIR else _indexer.REPO_PATH
                    ).as_posix()
                    _indexer.upsert_page(sconn, rel, abs_path)
                    n += 1
                _indexer.set_meta(sconn, "last_indexed_at", str(int(__import__("time").time())))
                sconn.commit()
                log.info("[workspaces] markdown rebuild for %s: %d pages", ws.name, n)
                pending = list(_indexer._pending_embeds)
                pending_images = list(_indexer._pending_images)
                _indexer._pending_embeds.clear()
                _indexer._pending_images.clear()
                if pending or pending_images:
                    await _indexer._flush_memory(pending, pending_images)
                    log.info("[workspaces] markdown embed flush: %d doc_chunks, "
                             "%d image refs", len(pending), len(pending_images))
            finally:
                try:
                    sconn.close()
                except Exception:
                    pass
    except Exception as e:
        log.warning("[workspaces] indexer refresh/rebuild failed: %s", e)

    # 5) persist
    workspace_state.write_active(ws.name)

    return {
        "name": ws.name,
        "docs_path": ws.docs_path,
        "backend": ws.backend,
        "dim": ws.dim,
        "pg_db": ws.pg_db,
        "hybrid": ws.hybrid,
        "db_created": db_created,
    }


def resolve_initial() -> Optional[Workspace]:
    """Pick the workspace to activate at boot.

    Preference order:
      1. Name persisted in the state file (if still in the config).
      2. First entry in workspaces.yaml.
      3. None → caller skips activation and uses legacy .env behavior.
    """
    from .workspaces import load_workspaces
    workspaces = load_workspaces()
    if not workspaces:
        return None
    persisted = workspace_state.read_active()
    if persisted:
        match = get_by_name(persisted)
        if match:
            return match
        log.warning("[workspaces] persisted active %r no longer in config; "
                    "falling back to first entry", persisted)
    return workspaces[0]


__all__ = ["activate", "resolve_initial"]
