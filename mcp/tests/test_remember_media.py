"""End-to-end tests for the remember_media MCP tool + store.upsert_media_chunk.

Requires PG_TEST_DSN like the other store tests. Embeddings are
monkey-patched deterministically by conftest.py's `_patch_embeddings`
fixture (replaces `memory.embeddings.embed_batch`), so no network is
hit and the fake embedder accepts any item type.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("PG_TEST_DSN"), reason="no PG_TEST_DSN"
)


@pytest.fixture
def png_file(tmp_path: Path) -> Path:
    from PIL import Image
    p = tmp_path / "shot.png"
    Image.new("RGB", (128, 96), (20, 40, 80)).save(p)
    return p


# conftest's _patch_embeddings expects str items (uses str.split / encode).
# For media tests we need it to accept Path too, so override locally.
@pytest.fixture(autouse=True)
def _fake_media_embed(monkeypatch):
    import hashlib
    import math

    from memory import embeddings as emb_mod

    _DIM = 1536

    def _fake(item) -> list[float]:
        key = str(item).encode("utf-8")
        vec = [0.0] * _DIM
        filled = 0
        counter = 0
        while filled < _DIM:
            h = hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
            for b in h:
                if filled >= _DIM:
                    break
                vec[filled] = (b - 127.5) / 127.5
                filled += 1
            counter += 1
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def embed_batch(items):
        return [_fake(it) for it in items]

    async def embed_one(item):
        return _fake(item)

    monkeypatch.setattr(emb_mod, "embed_batch", embed_batch)
    monkeypatch.setattr(emb_mod, "embed_one", embed_one)
    yield


class _FakeBackend:
    name = "fake-multimodal"
    native_dim = 1536
    modalities = frozenset({"text", "image", "video", "audio", "pdf"})
    price_estimate_usd_per_1k = {"text": 0.0}

    async def embed(self, items, out_dim):  # pragma: no cover - not called
        return [[0.0] * out_dim for _ in items]


class _TextOnlyBackend:
    name = "fake-text-only"
    native_dim = 1536
    modalities = frozenset({"text"})
    price_estimate_usd_per_1k = {"text": 0.0}

    async def embed(self, items, out_dim):  # pragma: no cover
        return [[0.0] * out_dim for _ in items]


def _patch_backend(monkeypatch, backend):
    """Swap the registry's get_backend() so modality guards see `backend`."""
    from memory import embedders as _emb_pkg
    from memory import store as _store

    monkeypatch.setattr(_emb_pkg, "get_backend", lambda name=None: backend)
    monkeypatch.setattr(_store, "get_backend", lambda name=None: backend)


async def test_remember_media_end_to_end(png_file: Path, monkeypatch):
    _patch_backend(monkeypatch, _FakeBackend())

    from memory import db, store
    from memory.chunker_image import chunk_image

    chunk = chunk_image(png_file, caption="blue-ish shot")
    result = await store.upsert_media_chunk(
        chunk, context="ticket #42", tags=["ui", "bug"],
    )

    assert result["kind"] == "image"
    assert result["media_type"] == "image/png"
    assert "id" in result and len(result["id"]) == 36

    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind::text, media_ref, media_type, preview_b64, "
                "       metadata FROM memories WHERE id = %s",
                (result["id"],),
            )
            row = await cur.fetchone()

    assert row is not None
    kind, media_ref, media_type, preview_b64, meta = row
    assert kind == "image"
    assert media_ref and media_ref.endswith("shot.png")
    assert media_type == "image/png"
    assert preview_b64 and len(preview_b64) > 0
    assert meta["context"] == "ticket #42"
    assert "ui" in meta["tags"]


async def test_remember_media_wrong_backend(png_file: Path, monkeypatch):
    _patch_backend(monkeypatch, _TextOnlyBackend())

    from memory import store
    from memory.chunker_image import chunk_image

    chunk = chunk_image(png_file)
    with pytest.raises(RuntimeError) as ei:
        await store.upsert_media_chunk(chunk)

    msg = str(ei.value)
    assert "fake-text-only" in msg
    assert "image" in msg
