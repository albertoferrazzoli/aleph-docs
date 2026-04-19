"""Gemini text-only embedder (default backend).

Uses the `gemini-embedding-001` model via google-genai. Native dim is
1536 and MRL output_dimensionality lets callers truncate smaller if
desired. This is a straight port of the behaviour that used to live in
`memory/embeddings.py`.
"""

from __future__ import annotations

import logging
import os
from typing import List

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .base import Backend, BackendError, guard_out_dim

logger = logging.getLogger("memory")

EMBED_MODEL_ID = "gemini-embedding-001"
NATIVE_DIM = 1536


def _get_client():
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise BackendError(
            "gemini-001: GOOGLE_API_KEY is not set. Export it before calling embed()."
        )
    from google import genai  # type: ignore
    return genai.Client(api_key=api_key)


class GeminiTextBackend:
    name = "gemini-001"
    native_dim = NATIVE_DIM
    modalities = frozenset({"text"})
    price_estimate_usd_per_1k = {"text": 0.000075}

    async def embed(self, items: list, out_dim: int) -> List[List[float]]:
        if not items:
            return []
        # Guards run OUTSIDE tenacity so BackendError isn't wrapped and retried.
        guard_out_dim(self.name, self.native_dim, out_dim)

        # gemini-001 is text-only: reject anything else up front.
        for i, it in enumerate(items):
            if not isinstance(it, str):
                raise BackendError(
                    f"gemini-001 is text-only but item[{i}] is {type(it).__name__}. "
                    f"Use EMBED_BACKEND=gemini-2-preview for multimodal."
                )
        return await self._embed_with_retry(items, out_dim)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _embed_with_retry(self, items: list, out_dim: int) -> List[List[float]]:
        model = os.environ.get("EMBED_MODEL") or EMBED_MODEL_ID
        logger.debug(
            "gemini-001.embed: items=%d model=%s out_dim=%d", len(items), model, out_dim
        )

        client = _get_client()
        from google.genai import types as genai_types  # type: ignore

        config = genai_types.EmbedContentConfig(output_dimensionality=out_dim)
        resp = await client.aio.models.embed_content(
            model=model,
            contents=items,
            config=config,
        )

        vectors: List[List[float]] = []
        for emb in resp.embeddings:
            values = getattr(emb, "values", None)
            if values is None and isinstance(emb, dict):
                values = emb.get("values")
            vectors.append(list(values) if values is not None else [])
        return vectors


BACKEND = GeminiTextBackend()
