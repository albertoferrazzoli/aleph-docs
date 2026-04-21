"""Async PostgreSQL connection pool for the semantic-memory layer.

Exposes a psycopg 3 AsyncConnectionPool wired with pgvector adapters. The pool
is opt-in via MEMORY_ENABLED + PG_DSN env vars so the rest of the MCP server
keeps working (backward compatible) even when Postgres is unavailable.
"""

from __future__ import annotations

import contextvars
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

log = logging.getLogger("memory")


class MemoryDisabled(RuntimeError):
    """Raised when a caller tries to acquire a connection while memory is off."""


_pool: Optional["AsyncConnectionPool"] = None  # type: ignore[name-defined]
_enabled: bool = False

# When a background task (typically an ingest) needs to pin its writes
# to a specific workspace even across runtime workspace switches, it
# sets this ContextVar to a dedicated pool. `get_conn()` will route
# through that override instead of the module singleton, so a mid-
# flight reconcile won't leak into a new workspace's DB when the user
# switches from the viewer.
_override_pool_var: contextvars.ContextVar[Optional["AsyncConnectionPool"]] = \
    contextvars.ContextVar("memory_db_override_pool", default=None)


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

    Routing, in order:
      1. If an override pool is set on the current ContextVar (via
         `pool_override()`), use that. This is what pins a background
         ingest to its originating workspace so a runtime workspace
         switch cannot redirect its writes.
      2. Otherwise use the module-level pool, self-healing it if it's
         been closed but env still says memory is enabled.

    Raises MemoryDisabled when no pool is available.
    """
    global _pool, _enabled

    override = _override_pool_var.get()
    if override is not None:
        async with override.connection() as conn:
            yield conn
            return

    if _pool is None:
        # Try to recover — cheap and idempotent.
        try:
            await init_pool()
        except Exception as e:
            raise MemoryDisabled(
                f"memory subsystem could not self-heal its pool: {e}"
            ) from e
    if not _enabled or _pool is None:
        raise MemoryDisabled("memory subsystem is disabled or not initialized")
    async with _pool.connection() as conn:
        yield conn


@asynccontextmanager
async def pool_override(dsn: str):
    """Pin all `get_conn()` calls inside this async task (and its
    children) to a dedicated pool bound to `dsn`, regardless of what
    the module-level pool points at.

    Use this to isolate a background reconcile from concurrent
    workspace switches — the task keeps writing to the database it
    started on even if the user flips the active workspace in the
    viewer halfway through.

    Safe to nest (inner override wins). Pool is created on enter and
    fully closed on exit, so this is NOT appropriate for tight loops
    — callers should wrap entire task bodies, not individual queries.
    """
    from psycopg_pool import AsyncConnectionPool

    max_size = max(1, int(os.getenv("PG_POOL_MAX", "4")) // 2)
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=max_size,
        configure=_configure_connection,
        open=False,
    )
    await pool.open()
    token = _override_pool_var.set(pool)
    try:
        yield pool
    finally:
        _override_pool_var.reset(token)
        try:
            await pool.close()
        except Exception as e:  # pragma: no cover
            log.warning("[memory] pool_override close failed: %s", e)


async def health_check() -> dict:
    """Return a structured health report. Never raises.

    Same self-heal as get_conn(): when env says memory is on but the
    pool has been nuked (workspace switch race, LISTEN/NOTIFY crash,
    etc.) we try to re-init before reporting degraded.
    """
    # Self-heal before reporting disabled.
    if _pool is None:
        try:
            await init_pool()
        except Exception as e:
            log.warning("[memory] health_check: pool re-init failed: %s", e)

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
