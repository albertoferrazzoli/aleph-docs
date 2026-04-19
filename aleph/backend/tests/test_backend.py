"""Tests for the Aleph backend.

Requires PG_TEST_DSN set to a Postgres DB with:
    CREATE EXTENSION vector;
    CREATE EXTENSION pgcrypto;
    \\i ../../mcp/memory/schema.sql
    \\i ../schema_additions.sql
    \\i ../triggers.sql
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest


PG_TEST_DSN = os.environ.get("PG_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not PG_TEST_DSN, reason="PG_TEST_DSN not set")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert body["status"] in {"ok", "degraded"}
    assert "memory_count" in body
    assert "snapshot_version" in body
    assert "graph_nodes" in body


def test_graph_empty(client):
    r = client.get("/graph")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 0
    assert body["nodes"] == []
    assert body["edges"] == []


def test_remember_auth(client):
    # no key → 401
    r = client.post("/remember", json={"content": "Hello Aleph"})
    assert r.status_code == 401

    # wrong key → 401
    r = client.post(
        "/remember",
        json={"content": "Hello Aleph"},
        headers={"X-Aleph-Key": "wrong"},
    )
    assert r.status_code == 401

    # correct key → 200 + id
    r = client.post(
        "/remember",
        json={"content": "Hello Aleph", "tags": ["test"]},
        headers={"X-Aleph-Key": os.environ["ALEPH_API_KEY"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert "id" in body
    # uuid-parseable
    uuid.UUID(body["id"])


def test_search(client):
    # empty DB → empty list; after inserting a row search should still work.
    headers = {"X-Aleph-Key": os.environ["ALEPH_API_KEY"]}
    client.post(
        "/remember",
        json={"content": "pgvector cosine similarity pool"},
        headers=headers,
    )
    r = client.post("/search", json={"query": "pgvector", "min_score": 0.0})
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    assert isinstance(body["results"], list)
    assert "count" in body


def test_projection_writes_snapshot():
    try:
        import umap  # noqa: F401
        import hdbscan  # noqa: F401
    except Exception as e:
        pytest.skip(f"umap-learn/hdbscan not installed: {e}")

    from fastapi.testclient import TestClient
    from backend import main as main_module
    from backend import projection

    headers = {"X-Aleph-Key": os.environ["ALEPH_API_KEY"]}
    contents = [
        "example search flow",
        "semantic memory forgetting curve",
        "pgvector cosine neighbor search",
        "UMAP 3D projection dimensionality reduction",
        "HDBSCAN clustering algorithm density",
        "Aleph backend FastAPI endpoints",
    ]

    # 1) Seed data through the API.
    with TestClient(main_module.app) as c:
        for text in contents:
            r = c.post("/remember", json={"content": text}, headers=headers)
            assert r.status_code == 200

    # 2) Run projection in its own event loop (pool owned here).
    version = asyncio.run(projection.main())
    assert version >= 1

    # 3) Re-open TestClient: lifespan spins up a fresh pool bound to this loop.
    with TestClient(main_module.app) as c:
        r = c.get("/graph")
        body = r.json()
        assert body["version"] == version
        assert len(body["nodes"]) == len(contents)
        n0 = body["nodes"][0]
        for key in ["id", "kind", "content", "embedding_3d",
                    "created_at", "access_count", "stability"]:
            assert key in n0
        assert len(n0["embedding_3d"]) == 3
