"""Local / offline multimodal embedder via a host-side Nomic server.

Mirrors the `whisper_local` host-bridge pattern: a tiny FastAPI server
runs on the host (see `docker/nomic-embed-server/`) loading
`nomic-embed-text-v1.5` and `nomic-embed-vision-v1.5` into a shared
768-dim latent space, exposing two HTTP endpoints:

    POST /embed/text    {"text": "..."}             -> {"vector": [768 float]}
    POST /embed/image   multipart file=@<path>      -> {"vector": [768 float]}

The reason the model is not in-container: torch + weights (~2 GB) are
too heavy to ship inside the `mcp` image. Host-side MPS (Apple Silicon)
yields ~50 ms/request; the mcp container reaches the server via
`host.docker.internal`.

Env:
    EMBED_NOMIC_HOST      default http://host.docker.internal:8091
    EMBED_NOMIC_TIMEOUT_S default 60
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

import httpx

from .base import Backend, BackendError, guard_out_dim

logger = logging.getLogger("memory")

NATIVE_DIM = 768

# Suffixes routed to /embed/image. Matches the image MIMEs accepted by
# `memory.media._SUFFIX_TO_MIME` — keep the two in sync.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _host() -> str:
    return os.environ.get(
        "EMBED_NOMIC_HOST", "http://host.docker.internal:8091"
    ).rstrip("/")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("EMBED_NOMIC_TIMEOUT_S", "60"))
    except ValueError:
        return 60.0


class NomicMultimodalLocalBackend:
    name = "nomic_multimodal_local"
    native_dim = NATIVE_DIM
    modalities = frozenset({"text", "image"})
    price_estimate_usd_per_1k = {"text": 0.0, "image": 0.0}

    async def embed(self, items: list, out_dim: int) -> List[List[float]]:
        if not items:
            return []
        guard_out_dim(self.name, self.native_dim, out_dim)
        if out_dim != self.native_dim:
            # Nomic models are MRL-capable but we don't expose truncation
            # here — matches the `local.py` constraint for simplicity.
            raise BackendError(
                f"nomic_multimodal_local: out_dim={out_dim} != "
                f"native_dim={self.native_dim}. Set EMBED_DIM={self.native_dim}."
            )

        host = _host()
        text_url = f"{host}/embed/text"
        image_url = f"{host}/embed/image"
        timeout = _timeout_s()
        logger.debug(
            "nomic_multimodal_local.embed: items=%d host=%s", len(items), host,
        )

        vectors: List[List[float]] = []
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for i, item in enumerate(items):
                    if isinstance(item, str):
                        vec = await self._embed_text(client, text_url, item)
                    elif isinstance(item, Path):
                        if item.suffix.lower() not in _IMAGE_SUFFIXES:
                            raise BackendError(
                                f"nomic_multimodal_local: item[{i}] path "
                                f"{item} has unsupported suffix "
                                f"{item.suffix!r}; only {sorted(_IMAGE_SUFFIXES)} "
                                f"are routed to /embed/image."
                            )
                        if not item.is_file():
                            raise BackendError(
                                f"nomic_multimodal_local: item[{i}] path not "
                                f"found: {item}"
                            )
                        vec = await self._embed_image(client, image_url, item)
                    else:
                        raise BackendError(
                            f"nomic_multimodal_local: item[{i}] has "
                            f"unsupported type {type(item).__name__}; only "
                            f"str (text) and pathlib.Path (image) are accepted."
                        )
                    if len(vec) != self.native_dim:
                        raise BackendError(
                            f"nomic_multimodal_local: server returned "
                            f"dim={len(vec)}, expected native_dim="
                            f"{self.native_dim}."
                        )
                    vectors.append(vec)
        except (httpx.ConnectError, ConnectionError) as e:
            raise BackendError(
                f"nomic_multimodal_local unreachable at {host}: start the "
                f"host server (see docs/EMBED_NOMIC_SETUP.md) or switch "
                f"EMBED_BACKEND. ({e})"
            ) from e
        except httpx.HTTPError as e:
            raise BackendError(
                f"nomic_multimodal_local HTTP error at {host}: {e}"
            ) from e

        return vectors

    async def _embed_text(
        self, client: httpx.AsyncClient, url: str, text: str,
    ) -> List[float]:
        r = await client.post(url, json={"text": text})
        if r.status_code != 200:
            raise BackendError(
                f"nomic_multimodal_local: {url} returned HTTP "
                f"{r.status_code}: {r.text[:200]}"
            )
        return _extract_vector(r.json())

    async def _embed_image(
        self, client: httpx.AsyncClient, url: str, path: Path,
    ) -> List[float]:
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            r = await client.post(url, files=files)
        if r.status_code != 200:
            raise BackendError(
                f"nomic_multimodal_local: {url} returned HTTP "
                f"{r.status_code}: {r.text[:200]}"
            )
        return _extract_vector(r.json())


def _extract_vector(payload) -> List[float]:
    if not isinstance(payload, dict) or "vector" not in payload:
        raise BackendError(
            f"nomic_multimodal_local: unexpected response shape: "
            f"{type(payload).__name__}"
        )
    vec = payload["vector"]
    if not isinstance(vec, list):
        raise BackendError(
            f"nomic_multimodal_local: 'vector' is not a list "
            f"({type(vec).__name__})"
        )
    return [float(x) for x in vec]


BACKEND = NomicMultimodalLocalBackend()
