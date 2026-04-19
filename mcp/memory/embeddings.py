"""Thin backward-compat shim over the embedder registry.

This module used to contain the Gemini text-only client inline. It is
now a tiny forwarding layer over `memory.embedders.get_backend()` so
that existing callers (store, bootstrap, tests via conftest) keep
working unchanged:

    from memory import embeddings
    await embeddings.embed_batch(["hello"])
    await embeddings.embed_one("hello")
    embeddings.tokens_used()
    embeddings.reset_token_counter()

The active backend is resolved from `EMBED_BACKEND` (default
`gemini-001`) on first use and cached on the module. The target vector
dimension is read from `EMBED_DIM` (default 1536) and guarded against
the backend's `native_dim` before any API call.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from .embedders import Backend, BackendError, get_backend
from .embedders.base import guard_out_dim

logger = logging.getLogger("memory")

_tokens_used: int = 0
_backend: Backend | None = None


# ---------------------------------------------------------------------------
# Token counter (heuristic; retained for bootstrap cost estimates)
# ---------------------------------------------------------------------------


def tokens_used() -> int:
    return _tokens_used


def reset_token_counter() -> None:
    global _tokens_used
    _tokens_used = 0


def _estimate_tokens(items: list) -> int:
    est = 0
    for it in items:
        if isinstance(it, str):
            est += len(it) // 4
        elif isinstance(it, (bytes, bytearray)):
            est += max(1, len(it) // 1000)
        elif isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], (bytes, bytearray)):
            est += max(1, len(it[0]) // 1000)
        elif isinstance(it, Path):
            try:
                est += max(1, it.stat().st_size // 1000)
            except OSError:
                est += 1
        else:
            # Parts, etc. — conservative 1 "token" equivalent
            est += 1
    return est


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def _get_embed_dim() -> int:
    raw = os.environ.get("EMBED_DIM", "1536")
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid EMBED_DIM=%r, falling back to 1536", raw)
        return 1536


def _resolve_backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = get_backend()
        logger.info("memory.embeddings: using backend %s (native_dim=%d)",
                    _backend.name, _backend.native_dim)
    return _backend


# ---------------------------------------------------------------------------
# Public API — unchanged signatures
# ---------------------------------------------------------------------------


async def embed_batch(texts: list) -> List[List[float]]:
    """Embed a batch of items.

    Historical callers pass `list[str]`. New callers may pass any item
    type supported by the active backend (see embedders/base.py).
    """
    global _tokens_used
    if not texts:
        return []

    backend = _resolve_backend()
    out_dim = _get_embed_dim()

    # Fail fast BEFORE spending any API call.
    guard_out_dim(backend.name, backend.native_dim, out_dim)

    est = _estimate_tokens(texts)
    _tokens_used += est
    logger.debug(
        "embed_batch: backend=%s size=%d est_tokens=%d out_dim=%d",
        backend.name, len(texts), est, out_dim,
    )

    return await backend.embed(texts, out_dim)


async def embed_one(text) -> List[float]:
    """Embed a single item. Thin wrapper over embed_batch()."""
    out = await embed_batch([text])
    return out[0] if out else []
