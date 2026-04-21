"""Embedder backend registry.

Resolve the active backend at runtime via `get_backend()`. The name is
taken from the `EMBED_BACKEND` env var when not passed explicitly. See
PRD_MULTIMODAL.md §5.0 for the design contract.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Dict

from .base import Backend, BackendError

logger = logging.getLogger("memory")

# name → fully-qualified module path. Kept as strings so importing this
# package does not drag all backend deps (genai, httpx) into memory if
# the caller only wants one.
_REGISTRY: Dict[str, str] = {
    "gemini-001": "memory.embedders.gemini_001",
    "gemini-2-preview": "memory.embedders.gemini_2",
    "local": "memory.embedders.local",
    "nomic_multimodal_local": "memory.embedders.nomic_multimodal_local",
}

DEFAULT_BACKEND = "gemini-001"

_cache: Dict[str, Backend] = {}


def list_backends() -> list[str]:
    """Return registered backend names (sorted for stable output)."""
    return sorted(_REGISTRY.keys())


def get_backend(name: str | None = None) -> Backend:
    """Resolve a backend by name, defaulting to `EMBED_BACKEND` env (or
    `gemini-001`). Raises `BackendError` for unknown names.
    """
    if name is None:
        name = os.environ.get("EMBED_BACKEND", DEFAULT_BACKEND).strip() or DEFAULT_BACKEND

    if name in _cache:
        return _cache[name]

    mod_path = _REGISTRY.get(name)
    if not mod_path:
        raise BackendError(
            f"Unknown EMBED_BACKEND={name!r}. Valid names: {list_backends()}"
        )

    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        raise BackendError(
            f"Failed to import backend {name!r} from {mod_path}: {e}"
        ) from e

    backend = getattr(mod, "BACKEND", None)
    if backend is None:
        raise BackendError(
            f"Backend module {mod_path} does not expose a BACKEND instance."
        )
    logger.debug("embedders.get_backend: resolved %s -> %s", name, mod_path)
    _cache[name] = backend
    return backend


def _reset_cache_for_tests() -> None:
    """Test helper: drop cached backends so monkeypatched env re-resolves."""
    _cache.clear()


__all__ = [
    "Backend",
    "BackendError",
    "get_backend",
    "list_backends",
    "DEFAULT_BACKEND",
]
