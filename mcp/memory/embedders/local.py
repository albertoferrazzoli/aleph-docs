"""Local / offline text embedder via Ollama.

No sentence-transformers fallback in this phase — Ollama only. To set up:

    # install Ollama (macOS / linux)
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull bge-m3                   # 1024-dim default
    # then:
    EMBED_BACKEND=local EMBED_DIM=1024 LOCAL_EMBED_DIM=1024 ...

Env:
    OLLAMA_HOST       default http://127.0.0.1:11434
    OLLAMA_MODEL      default bge-m3
    LOCAL_EMBED_DIM   default 1024   (must match the model's output dim)
"""

from __future__ import annotations

import logging
import os
from typing import List

import httpx

from .base import Backend, BackendError, guard_out_dim

logger = logging.getLogger("memory")


def _native_dim() -> int:
    try:
        return int(os.environ.get("LOCAL_EMBED_DIM", "1024"))
    except ValueError:
        return 1024


def _host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _model() -> str:
    return os.environ.get("OLLAMA_MODEL", "bge-m3")


class LocalBackend:
    name = "local"
    # `native_dim` is snapshot at import (per PRD) but read via attribute so
    # tests that monkeypatch LOCAL_EMBED_DIM can still inspect it.
    native_dim = _native_dim()
    modalities = frozenset({"text"})
    price_estimate_usd_per_1k = {"text": 0.0}

    async def embed(self, items: list, out_dim: int) -> List[List[float]]:
        if not items:
            return []
        guard_out_dim(self.name, self.native_dim, out_dim)
        if out_dim != self.native_dim:
            # No MRL support for Ollama embed models in this phase.
            raise BackendError(
                f"local: out_dim={out_dim} != native_dim={self.native_dim}. "
                f"Ollama models do not support MRL truncation here; set "
                f"EMBED_DIM={self.native_dim}."
            )

        for i, it in enumerate(items):
            if not isinstance(it, str):
                raise BackendError(
                    f"local backend is text-only; item[{i}] is {type(it).__name__}"
                )

        host = _host()
        model = _model()
        url = f"{host}/api/embeddings"
        logger.debug("local.embed: items=%d host=%s model=%s", len(items), host, model)

        vectors: List[List[float]] = []
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                for text in items:
                    r = await client.post(url, json={"model": model, "prompt": text})
                    r.raise_for_status()
                    data = r.json()
                    vec = data.get("embedding") or data.get("embeddings") or []
                    if isinstance(vec, list) and vec and isinstance(vec[0], list):
                        # Some ollama variants return [[...]]
                        vec = vec[0]
                    if len(vec) != self.native_dim:
                        raise BackendError(
                            f"local: model {model} returned dim={len(vec)}, "
                            f"expected native_dim={self.native_dim}. "
                            f"Set LOCAL_EMBED_DIM to match."
                        )
                    vectors.append([float(x) for x in vec])
        except (httpx.ConnectError, ConnectionError) as e:
            raise BackendError(
                f"local backend unreachable at {host}: start Ollama or switch EMBED_BACKEND. "
                f"({e})"
            ) from e
        except httpx.HTTPError as e:
            raise BackendError(f"local backend HTTP error at {host}: {e}") from e

        return vectors


BACKEND = LocalBackend()
