"""Async PostgreSQL connection pool for the semantic-memory layer.

Exposes a psycopg 3 AsyncConnectionPool wired with pgvector adapters. The pool
is opt-in via MEMORY_ENABLED + PG_DSN env vars so the rest of the MCP server
keeps working (backward compatible) even when Postgres is unavailable.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

log = logging.getLogger("memory")


class MemoryDisabled(RuntimeError):
    """Raised when a caller tries to acquire a connection while memory is off."""


_pool: Optional["AsyncConnectionPool"] = None  # type: ignore[name-defined]
_enabled: bool = False


def _read_env() -> tuple[bool, str]:
    enabled = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
    dsn = os.getenv("PG_DSN", "").strip()
    return (enabled and bool(dsn)), dsn


def is_enabled() -> bool:
    """Whether the memory subsystem is active (env toggles + DSN present).

    Reads env directly so callers don't need to have invoked `init_pool()`
    first (e.g. bootstrap/indexer check the flag before opening the pool).
    """
    enabled, _ = _read_env()
    return enabled


async def _configure_connection(conn) -> None:
    """Per-connection configure hook: register pgvector adapters."""
    from pgvector.psycopg import register_vector_async

    await register_vector_async(conn)


async def init_pool() -> None:
    """Initialize the async connection pool.

    No-op if memory is disabled. Safe to call multiple times.
    """
    global _pool, _enabled

    enabled, dsn = _read_env()
    _enabled = enabled

    if not enabled:
        log.info("[memory] disabled (MEMORY_ENABLED != 'true' or PG_DSN empty)")
        return

    if _pool is not None:
        log.debug("[memory] pool already initialized")
        return

    from psycopg_pool import AsyncConnectionPool

    max_size = int(os.getenv("PG_POOL_MAX", "10"))

    # open=False so we can await pool.open() — configure hook is async.
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=max_size,
        configure=_configure_connection,
        open=False,
    )
    await pool.open()
    _pool = pool
    log.info("[memory] pool initialized (max_size=%d)", max_size)


async def close_pool() -> None:
    """Close and release the pool (idempotent)."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception as e:  # pragma: no cover
        log.warning("[memory] error closing pool: %s", e)
    finally:
        _pool = None
        log.info("[memory] pool closed")


@asynccontextmanager
async def get_conn():
    """Async context manager yielding a pooled connection.

    Raises MemoryDisabled if the memory subsystem is off or the pool was
    never initialized.
    """
    if not _enabled or _pool is None:
        raise MemoryDisabled("memory subsystem is disabled or not initialized")
    async with _pool.connection() as conn:
        yield conn


async def health_check() -> dict:
    """Return a structured health report. Never raises."""
    if not _enabled:
        return {
            "enabled": False,
            "ok": True,
            "memory_count": None,
            "error": None,
        }

    if _pool is None:
        return {
            "enabled": True,
            "ok": False,
            "memory_count": None,
            "error": "pool not initialized",
        }

    try:
        async with _pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM memories")
                row = await cur.fetchone()
                count = int(row[0]) if row else 0
        return {
            "enabled": True,
            "ok": True,
            "memory_count": count,
            "error": None,
        }
    except Exception as e:
        log.warning("[memory] health check failed: %s", e)
        return {
            "enabled": True,
            "ok": False,
            "memory_count": None,
            "error": str(e),
        }
