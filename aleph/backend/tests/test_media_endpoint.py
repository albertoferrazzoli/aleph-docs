"""Tests for GET /media/{memory_id}.

Uses the existing `client` fixture from conftest (which requires
PG_TEST_DSN). We insert a synthetic memory row with a `media_ref`
pointing at a tempfile under /tmp (allowed by the default MEDIA_ROOT
escape hatch), then hit the endpoint and assert streaming + traversal
refusal.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

PG_TEST_DSN = os.environ.get("PG_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not PG_TEST_DSN, reason="PG_TEST_DSN not set")


def _insert_media_row(media_ref: str, media_type: str) -> str:
    """Insert a bare memories row carrying only the media_* fields, return id."""
    import asyncio
    import psycopg

    mid = str(uuid.uuid4())
    zero_vec = "[" + ",".join(["0"] * 1536) + "]"

    async def _do():
        async with await psycopg.AsyncConnection.connect(PG_TEST_DSN) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO memories "
                    "(id, kind, content, embedding, media_ref, media_type) "
                    "VALUES (%s, %s, %s, %s::vector, %s, %s)",
                    (mid, "image", "test media row", zero_vec, media_ref, media_type),
                )
            await conn.commit()

    asyncio.run(_do())
    return mid


def test_media_streams_file(client):
    with tempfile.NamedTemporaryFile(
        prefix="aleph-test-", suffix=".png", delete=False,
    ) as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"test-bytes-0123456789")
        path = f.name
    try:
        mid = _insert_media_row(path, "image/png")
        r = client.get(f"/media/{mid}")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/png")
        assert b"test-bytes-0123456789" in r.content
    finally:
        Path(path).unlink(missing_ok=True)


def test_media_path_traversal_refused(client):
    mid = _insert_media_row("/etc/passwd", "text/plain")
    r = client.get(f"/media/{mid}")
    # /etc/passwd is a real file but not under MEDIA_ROOT or /tmp → 403.
    assert r.status_code in (403, 404), r.text


def test_media_missing_row(client):
    r = client.get(f"/media/{uuid.uuid4()}")
    assert r.status_code == 404


def test_media_null_ref(client):
    """A row with no media_ref should surface 404, not 500."""
    import asyncio
    import psycopg

    mid = str(uuid.uuid4())
    zero_vec = "[" + ",".join(["0"] * 1536) + "]"

    async def _insert():
        async with await psycopg.AsyncConnection.connect(PG_TEST_DSN) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO memories (id, kind, content, embedding) "
                    "VALUES (%s, %s, %s, %s::vector)",
                    (mid, "doc_chunk", "no media here", zero_vec),
                )
            await conn.commit()

    asyncio.run(_insert())
    r = client.get(f"/media/{mid}")
    assert r.status_code == 404


def test_media_with_fragment_strips_correctly(client):
    """media_ref='/tmp/foo.pdf#page=2' must resolve /tmp/foo.pdf."""
    with tempfile.NamedTemporaryFile(
        prefix="aleph-test-", suffix=".pdf", delete=False,
    ) as f:
        f.write(b"%PDF-1.4\n% fake\n")
        path = f.name
    try:
        mid = _insert_media_row(f"{path}#page=2", "application/pdf")
        r = client.get(f"/media/{mid}")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
    finally:
        Path(path).unlink(missing_ok=True)
