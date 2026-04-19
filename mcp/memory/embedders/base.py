"""Backend protocol and shared types for the embedder registry.

See PRD_MULTIMODAL.md §5.0. Every concrete backend under this package
implements the `Backend` Protocol below. `memory.embeddings` is a thin
shim that resolves one backend at runtime via `get_backend()`.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("memory")


class BackendError(RuntimeError):
    """Raised for any backend-level failure: bad config, wrong modality,
    dim mismatch, unreachable local service, etc.

    Callers upstream (store, bootstrap, tools) can distinguish these
    from unrelated exceptions.
    """


@runtime_checkable
class Backend(Protocol):
    """Uniform async embedder interface.

    Implementations live as modules under `memory.embedders.*` and
    expose a module-level `BACKEND` instance (or class) conforming to
    this shape. The registry in `memory.embedders.__init__` imports them
    lazily.
    """

    name: str                                  # "gemini-001", "gemini-2-preview", "local"
    native_dim: int                            # 1536 / 3072 / 1024 ...
    modalities: frozenset                      # {"text"} | {"text","image",...}
    price_estimate_usd_per_1k: dict            # per-modality hint

    async def embed(self, items: list, out_dim: int) -> list:
        """Embed a list of items. Every item is one of:
            str | bytes | (bytes, mime) | pathlib.Path | genai.types.Part

        Returns `len(items)` vectors of `out_dim` floats.
        Raises `BackendError` on modality / dim / transport failures.
        """
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def guard_out_dim(backend_name: str, native_dim: int, out_dim: int) -> None:
    """Fail fast if the caller asks for more dims than the backend can
    produce natively. MRL truncation is one-way: we can shrink, never grow.
    """
    if out_dim <= 0:
        raise BackendError(f"{backend_name}: out_dim must be positive (got {out_dim})")
    if out_dim > native_dim:
        raise BackendError(
            f"{backend_name}: requested out_dim={out_dim} exceeds native_dim={native_dim}. "
            f"Lower EMBED_DIM or switch backend."
        )
