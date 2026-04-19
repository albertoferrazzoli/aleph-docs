"""Decorator for auto-recording tool interactions.

Works on both sync and async callables. Failures never propagate.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading

from . import store

log = logging.getLogger("memory")


def _fire(query: str, top, tool_name: str) -> None:
    """Schedule async recording without blocking the caller."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(store.upsert_interaction(query, tool_name, top))
    except RuntimeError:
        # No running loop (sync call path).
        threading.Thread(
            target=lambda: asyncio.run(
                store.upsert_interaction(query, tool_name, top)
            ),
            daemon=True,
        ).start()


def _extract(args, kwargs) -> str:
    q = (
        kwargs.get("query")
        or kwargs.get("text")
        or kwargs.get("flag")
        or kwargs.get("key")
    )
    if not q and args:
        q = args[0] if isinstance(args[0], str) else ""
    return q or ""


def _top(result):
    if not isinstance(result, dict):
        return None
    for k in ("results", "pages", "code_matches"):
        lst = result.get(k)
        if lst:
            return lst[0]
    return None


def record_interaction(tool_name: str):
    """Wrap a tool function to auto-record the interaction after it runs."""

    def deco(fn):
        is_coro = inspect.iscoroutinefunction(fn)

        if is_coro:
            @functools.wraps(fn)
            async def aw(*args, **kw):
                result = await fn(*args, **kw)
                try:
                    _fire(_extract(args, kw), _top(result), tool_name)
                except Exception as e:
                    log.warning("[memory] interaction record failed: %s", e)
                return result

            return aw

        @functools.wraps(fn)
        def sw(*args, **kw):
            result = fn(*args, **kw)
            try:
                _fire(_extract(args, kw), _top(result), tool_name)
            except Exception as e:
                log.warning("[memory] interaction record failed: %s", e)
            return result

        return sw

    return deco
