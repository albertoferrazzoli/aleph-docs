"""Test fixtures for the Aleph backend.

Requires env var PG_TEST_DSN pointing to a Postgres DB with the pgvector
extension and the aleph-docs-mcp `memories` schema + Aleph additions already
loaded (see the Verification section in the task brief).

Mocks Gemini embeddings with a deterministic hash-based 1536-d vector so
tests don't need a live API key.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Path wiring: make `backend` importable when running pytest from aleph/
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve()
ALEPH_ROOT = HERE.parent.parent.parent  # aleph/
if str(ALEPH_ROOT) not in sys.path:
    sys.path.insert(0, str(ALEPH_ROOT))

# Force the MCP path before importing anything that touches it.
if not os.environ.get("MCP_PATH"):
    os.environ["MCP_PATH"] = str(
        (ALEPH_ROOT.parent / "mcp" / "aleph-docs-mcp").resolve()
    )
# Also make `memory` importable directly (conftest patches it by name).
_MCP_PATH = os.environ["MCP_PATH"]
if _MCP_PATH not in sys.path:
    sys.path.insert(0, _MCP_PATH)

# Tests require PG_TEST_DSN; also mirror it into PG_DSN + MEMORY_ENABLED so the
# underlying memory.db pool picks it up automatically.
PG_TEST_DSN = os.environ.get("PG_TEST_DSN", "").strip()
if PG_TEST_DSN:
    os.environ.setdefault("PG_DSN", PG_TEST_DSN)
    os.environ.setdefault("MEMORY_ENABLED", "true")
os.environ.setdefault("ALEPH_API_KEY", "test-key")


pytestmark_skip_no_db = pytest.mark.skipif(
    not PG_TEST_DSN,
    reason="PG_TEST_DSN not set; skipping DB-backed tests",
)


# ---------------------------------------------------------------------------
# Deterministic fake embeddings (no Gemini calls)
# ---------------------------------------------------------------------------

def _fake_vec(text: str, dim: int = 1536) -> list[float]:
    """Cheap deterministic embedding: hash-seeded pseudo-random floats."""
    import random
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    v = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    # L2 normalize
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


@pytest.fixture(autouse=True)
def _patch_embeddings(monkeypatch):
    if not PG_TEST_DSN:
        return
    # Import here so path wiring above has already run.
    from memory import embeddings  # type: ignore

    async def fake_embed_one(text: str) -> list[float]:
        return _fake_vec(text)

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [_fake_vec(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_one", fake_embed_one)
    monkeypatch.setattr(embeddings, "embed_batch", fake_embed_batch)


# ---------------------------------------------------------------------------
# DB cleanup between tests
# ---------------------------------------------------------------------------

async def _truncate():
    import psycopg
    async with await psycopg.AsyncConnection.connect(PG_TEST_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute("TRUNCATE TABLE memories RESTART IDENTITY CASCADE")
            await cur.execute("TRUNCATE TABLE graph_snapshot RESTART IDENTITY CASCADE")
        await conn.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    if not PG_TEST_DSN:
        yield
        return
    await _truncate()
    yield
    # leave data in place after the test for post-mortem if wanted


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    if not PG_TEST_DSN:
        pytest.skip("PG_TEST_DSN not set")
    from fastapi.testclient import TestClient
    from backend import main as main_module  # noqa

    # Lifespan context handles init_pool/close_pool.
    with TestClient(main_module.app) as c:
        yield c
