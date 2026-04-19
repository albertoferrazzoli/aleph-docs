"""Async client around google-genai for Gemini embeddings.

Exposes:
    embed_batch(texts)         -> list[list[float]]
    embed_one(text)            -> list[float]
    tokens_used()              -> cumulative heuristic token counter
    reset_token_counter()      -> reset counter (for tests)
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

logger = logging.getLogger("memory")

_tokens_used: int = 0


def tokens_used() -> int:
    """Return the cumulative heuristic token counter."""
    return _tokens_used


def reset_token_counter() -> None:
    """Reset the cumulative token counter. Intended for tests."""
    global _tokens_used
    _tokens_used = 0


def _estimate_tokens(texts: List[str]) -> int:
    return sum(len(t) // 4 for t in texts)


def _get_client():
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Export it in the environment "
            "before calling embed_batch()/embed_one()."
        )
    # Imported lazily so the module can be imported without google-genai
    # configured (e.g. in tests that don't touch the API).
    from google import genai  # type: ignore

    return genai.Client(api_key=api_key)


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
async def embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts in a single API call.

    Returns a list of 1536-dim vectors (MRL output_dimensionality).
    """
    global _tokens_used

    if not texts:
        return []

    model = os.environ.get("EMBED_MODEL", "gemini-embedding-001")
    try:
        dim = int(os.environ.get("EMBED_DIM", "1536"))
    except ValueError:
        dim = 1536

    est = _estimate_tokens(texts)
    _tokens_used += est
    logger.debug(
        "embed_batch: size=%d estimated_tokens=%d model=%s dim=%d",
        len(texts),
        est,
        model,
        dim,
    )

    client = _get_client()

    # google-genai async embed call. Pass contents=texts for batch.
    # output_dimensionality enables MRL truncation to `dim`.
    from google.genai import types as genai_types  # type: ignore

    config = genai_types.EmbedContentConfig(output_dimensionality=dim)
    resp = await client.aio.models.embed_content(
        model=model,
        contents=texts,
        config=config,
    )

    # Response has .embeddings: list[ContentEmbedding(values=...)]
    vectors: List[List[float]] = []
    for emb in resp.embeddings:
        values = getattr(emb, "values", None)
        if values is None and isinstance(emb, dict):
            values = emb.get("values")
        vectors.append(list(values) if values is not None else [])
    return vectors


async def embed_one(text: str) -> List[float]:
    """Embed a single text. Thin wrapper over embed_batch()."""
    out = await embed_batch([text])
    return out[0] if out else []
