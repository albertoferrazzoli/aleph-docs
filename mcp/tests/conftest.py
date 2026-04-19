"""Shared pytest fixtures for aleph-docs-mcp memory tests."""

from __future__ import annotations

import hashlib
import math
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make the package importable when running pytest from the repo root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


pytest_plugins = ["pytest_asyncio"]


# ---------------------------------------------------------------------------
# Deterministic fake embedder
# ---------------------------------------------------------------------------

_DIM = 1536


def _fake_embed(text: str) -> list[float]:
    """Deterministic normalized pseudo-vector of dim 1536.

    Uses chained SHA256 digests to seed floats. Identical strings yield
    identical vectors. We also inject token-frequency bumps so similar
    texts yield high cosine similarity.
    """
    # Base vector from chained hashing
    vec = [0.0] * _DIM
    seed = text.encode("utf-8")
    filled = 0
    counter = 0
    while filled < _DIM:
        h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        # each byte -> float in [-1, 1]
        for b in h:
            if filled >= _DIM:
                break
            vec[filled] = (b - 127.5) / 127.5
            filled += 1
        counter += 1

    # Token-frequency bumps so "same words" texts get high cosine sim.
    for tok in set(text.lower().split()):
        th = hashlib.sha256(tok.encode("utf-8")).digest()
        idx = int.from_bytes(th[:4], "big") % _DIM
        # Strong bump so token overlap dominates base randomness
        vec[idx] += 50.0

    # Normalize
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@pytest.fixture(autouse=True)
def _patch_embeddings(monkeypatch):
    """Replace embeddings with deterministic fakes (no network)."""
    from memory import embeddings as emb_mod

    async def embed_batch(texts):
        return [_fake_embed(t) for t in texts]

    async def embed_one(text):
        return _fake_embed(text)

    monkeypatch.setattr(emb_mod, "embed_batch", embed_batch)
    monkeypatch.setattr(emb_mod, "embed_one", embed_one)

    # Also patch in store module (it imports embeddings module, so patching
    # the module attribute above is sufficient since store calls
    # `embeddings.embed_*`).
    yield


# ---------------------------------------------------------------------------
# DB pool fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _db_pool(monkeypatch):
    """Initialize the memory pool against PG_TEST_DSN and wipe between tests."""
    dsn = os.getenv("PG_TEST_DSN")
    if not dsn:
        yield
        return

    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("PG_DSN", dsn)

    from memory import db

    # Force fresh pool for this test module
    await db.close_pool()
    await db.init_pool()

    # Wipe tables (memory_audit may not exist on older test DBs; tolerate).
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE memories RESTART IDENTITY")
            await cur.execute(
                "DO $$ BEGIN "
                "IF to_regclass('public.memory_audit') IS NOT NULL THEN "
                "  EXECUTE 'TRUNCATE memory_audit RESTART IDENTITY'; "
                "END IF; END $$;"
            )
        await conn.commit()

    yield

    await db.close_pool()
