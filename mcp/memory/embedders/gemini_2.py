"""Gemini multimodal embedder (preview).

Uses `gemini-embedding-2-preview` (overridable via EMBED_MODEL for a
future GA rename). Accepts text, images, video, audio, PDF. See
PRD_MULTIMODAL.md §5.3.

Items may be:
    * str                      -> embedded as text
    * pathlib.Path             -> read bytes, MIME detected by suffix, wrapped in a Part
    * (bytes, mime)            -> tuple; wrapped in a Part with the given mime_type
    * google.genai.types.Part  -> passed through
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import List

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import Backend, BackendError, guard_out_dim

logger = logging.getLogger("memory")

EMBED_MODEL_ID = "gemini-embedding-2-preview"
NATIVE_DIM = 3072


# Extension → MIME fallback for a few common types the stdlib may miss.
_MIME_FALLBACK = {
    ".heic": "image/heic",
    ".webp": "image/webp",
    ".m4a": "audio/mp4",
    ".mov": "video/quicktime",
}


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    return _MIME_FALLBACK.get(path.suffix.lower(), "application/octet-stream")


def _get_client():
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise BackendError(
            "gemini-2-preview: GOOGLE_API_KEY is not set. Export it before calling embed()."
        )
    from google import genai  # type: ignore
    return genai.Client(api_key=api_key)


def _to_part(item, idx: int):
    """Normalise one item to a google.genai Part (or keep as str)."""
    from google.genai import types as genai_types  # type: ignore

    if isinstance(item, str):
        return item
    if isinstance(item, genai_types.Part):
        return item
    if isinstance(item, Path):
        if not item.is_file():
            raise BackendError(f"gemini-2-preview: item[{idx}] path not found: {item}")
        data = item.read_bytes()
        mime = _guess_mime(item)
        return genai_types.Part.from_bytes(data=data, mime_type=mime)
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], (bytes, bytearray)):
        data, mime = item
        if not isinstance(mime, str):
            raise BackendError(
                f"gemini-2-preview: item[{idx}] tuple must be (bytes, mime:str)"
            )
        return genai_types.Part.from_bytes(data=bytes(data), mime_type=mime)
    if isinstance(item, (bytes, bytearray)):
        raise BackendError(
            f"gemini-2-preview: item[{idx}] is raw bytes — wrap as (bytes, mime) or pass a Path."
        )
    raise BackendError(
        f"gemini-2-preview: item[{idx}] has unsupported type {type(item).__name__}"
    )


class GeminiMultimodalBackend:
    name = "gemini-2-preview"
    native_dim = NATIVE_DIM
    modalities = frozenset({"text", "image", "video", "audio", "pdf"})
    # Placeholders; update when Google publishes final pricing.
    price_estimate_usd_per_1k = {
        "text": 0.00015,
        "image": 0.00025,
        "video": 0.00100,
        "audio": 0.00050,
        "pdf": 0.00025,
    }

    async def embed(self, items: list, out_dim: int) -> List[List[float]]:
        if not items:
            return []
        guard_out_dim(self.name, self.native_dim, out_dim)
        parts = [_to_part(it, i) for i, it in enumerate(items)]
        return await self._embed_with_retry(parts, out_dim)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _embed_with_retry(self, parts: list, out_dim: int) -> List[List[float]]:
        model = os.environ.get("EMBED_MODEL") or EMBED_MODEL_ID
        logger.debug(
            "gemini-2-preview.embed: items=%d model=%s out_dim=%d",
            len(parts), model, out_dim,
        )

        client = _get_client()
        from google.genai import types as genai_types  # type: ignore

        config = genai_types.EmbedContentConfig(output_dimensionality=out_dim)
        resp = await client.aio.models.embed_content(
            model=model,
            contents=parts,
            config=config,
        )

        vectors: List[List[float]] = []
        for emb in resp.embeddings:
            values = getattr(emb, "values", None)
            if values is None and isinstance(emb, dict):
                values = emb.get("values")
            vectors.append(list(values) if values is not None else [])
        return vectors


BACKEND = GeminiMultimodalBackend()
