"""Tests for memory.store — require a Postgres test DB via PG_TEST_DSN."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("PG_TEST_DSN"), reason="no PG_TEST_DSN"
)


async def _count():
    from memory import store
    return await store.count_by_kind()


async def test_count_empty():
    c = await _count()
    assert c == {"doc_chunk": 0, "interaction": 0, "insight": 0, "total": 0}


async def test_insert_insight_and_search():
    from memory import store
    texts = [
        "floating license revocation procedure step by step",
        "how to compile strong named assemblies with snk keys",
        "unicorns rainbows glitter cupcakes party",
    ]
    for t in texts:
        await store.insert_insight(t)

    c = await _count()
    assert c["insight"] == 3

    results = await store.search(texts[0], min_score=0.0)
    assert results, "expected at least one result"
    assert results[0]["content"] == texts[0]
    assert results[0]["score"] > 0.5


async def test_reinforce_on_search():
    from memory import store
    q = "server configuration and setup"
    await store.insert_insight(q)

    r1 = await store.search(q, min_score=0.0)
    assert r1
    assert r1[0]["access_count"] == 1  # 0 -> +1
    stab1 = r1[0]["stability"]

    r2 = await store.search(q, min_score=0.0)
    assert r2
    assert r2[0]["access_count"] == 2
    # stability starts at 14 (insight default), x1.7 twice ≈ 40.46
    assert r2[0]["stability"] > stab1
    assert abs(r2[0]["stability"] - 14 * 1.7 * 1.7) < 0.5


async def test_interaction_dedup():
    from memory import store
    q = "how do I revoke a floating license"
    r1 = await store.upsert_interaction(q, "search_docs", None)
    assert r1["action"] == "inserted"

    r2 = await store.upsert_interaction(q, "search_docs", None)
    assert r2["action"] == "reinforced"
    assert r2["similarity"] is not None and r2["similarity"] > 0.9

    c = await _count()
    assert c["interaction"] == 1

    # Verify access_count went to 2
    from memory import db
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT access_count FROM memories WHERE kind='interaction'"
            )
            row = await cur.fetchone()
    # initial insert: access_count=0; one reinforce → 1
    assert row[0] == 1


async def test_forget():
    from memory import store
    ins = await store.insert_insight("something to be forgotten soon")
    mid = ins["id"]
    res = await store.forget(mid)
    assert res["deleted"] is True

    results = await store.search("something to be forgotten soon", min_score=0.0)
    assert all(r["id"] != mid for r in results)


async def test_upsert_doc_chunks_idempotent():
    from memory import store
    from memory.chunker import Chunk

    def mk(anchor, body):
        content = f"# Title\n## {anchor}\n\n{body}"
        return Chunk(
            section_anchor=anchor,
            title="Title",
            content=content,
            hash=hashlib.sha256(content.encode()).hexdigest(),
            token_estimate=len(content) // 4,
            metadata={"source_path": "docs/test.md", "section_path": f"Title > {anchor}", "level": 2},
        )

    chunks = [mk("intro", "hello world"), mk("usage", "some usage example")]

    r1 = await store.upsert_doc_chunks("docs/test.md", chunks)
    assert r1["inserted"] == 2
    assert r1["skipped"] == 0

    r2 = await store.upsert_doc_chunks("docs/test.md", chunks)
    assert r2["inserted"] == 0
    assert r2["updated"] == 0
    assert r2["skipped"] == len(chunks)


async def _audit_rows(op: str | None = None, subject_id: str | None = None):
    from memory import db

    clauses = []
    params: list = []
    if op:
        clauses.append("op = %s")
        params.append(op)
    if subject_id:
        clauses.append("subject_id = %s")
        params.append(subject_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT op, subject_id, actor, kind, content, metadata "
                f"FROM memory_audit{where} ORDER BY id ASC",
                params,
            )
            rows = await cur.fetchall()
    return rows


async def test_insight_creates_audit_insert():
    from memory import store

    res = await store.insert_insight("audit trail insight test content")
    mid = res["id"]

    rows = await _audit_rows(op="insert", subject_id=mid)
    assert len(rows) == 1
    op, sid, actor, kind, content, metadata = rows[0]
    assert op == "insert"
    assert str(sid) == mid
    assert actor == "mcp:remember"
    assert kind == "insight"
    assert "audit trail insight" in content


async def test_forget_creates_audit_delete_with_snapshot():
    from memory import store

    original = "content that will be snapshotted before delete"
    ins = await store.insert_insight(original)
    mid = ins["id"]

    res = await store.forget(mid)
    assert res["deleted"] is True

    rows = await _audit_rows(subject_id=mid)
    ops = [r[0] for r in rows]
    assert ops == ["insert", "delete"]
    # Snapshot retained in the delete row
    del_row = rows[1]
    assert del_row[0] == "delete"
    assert del_row[3] == "insight"  # kind snapshot
    assert original in del_row[4]   # content snapshot


async def test_audit_tool_filters():
    """Exercise the audit_history tool logic via a stub mcp registry."""
    from memory import store
    import tools.memory as mem_tools

    # Collect registered tools into a dict via a minimal stub.
    registered: dict = {}

    class _StubMCP:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    mem_tools.register(_StubMCP())
    audit_history = registered["audit_history"]

    # Insert 3 distinct insights
    for i in range(3):
        await store.insert_insight(f"filter-test insight number {i} unique")

    res = await audit_history(op="insert", since_hours=168, limit=100)
    # count should include exactly the 3 inserts we made (fresh DB)
    assert res["count"] == 3
    assert all(e["op"] == "insert" for e in res["events"])

    # since_hours=0 returns nothing (window is in the past)
    res2 = await audit_history(op="insert", since_hours=0, limit=100)
    assert res2["count"] == 0


async def test_reinforce_gated_by_env(monkeypatch):
    from memory import store

    # Default: AUDIT_REINFORCE unset → no reinforce rows
    monkeypatch.delenv("AUDIT_REINFORCE", raising=False)
    q = "reinforce gating audit query example text"
    await store.insert_insight(q)
    await store.search(q, min_score=0.0)

    # Let any fire-and-forget tasks run
    import asyncio as _a
    await _a.sleep(0.1)

    rows = await _audit_rows(op="reinforce")
    assert rows == []

    # Now enable and search again
    monkeypatch.setenv("AUDIT_REINFORCE", "true")
    await store.search(q, min_score=0.0)
    await _a.sleep(0.2)

    rows2 = await _audit_rows(op="reinforce")
    assert len(rows2) >= 1
    assert rows2[0][2] == "store:search"  # actor


async def test_decay_affects_score():
    from memory import store
    from memory import db

    await store.insert_insight("this will decay heavily over time")

    # Push last_access_at into the past
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories SET last_access_at = now() - interval '60 days' "
                "WHERE kind='insight'"
            )
        await conn.commit()

    # With default min_score=0.15 and stability=14, decay ≈ 0.014 -> filtered
    results = await store.search("this will decay heavily over time")
    assert results == []

    # But with min_score=0 we still get it, and the decay is tiny
    results_raw = await store.search(
        "this will decay heavily over time", min_score=0.0
    )
    assert results_raw
    assert results_raw[0]["decay"] < 0.02
