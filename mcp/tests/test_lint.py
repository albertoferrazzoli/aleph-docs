"""Tests for memory.lint — require a Postgres test DB via PG_TEST_DSN.

All LLM calls are monkeypatched: no network is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("PG_TEST_DSN"), reason="no PG_TEST_DSN"
)


# Ensure the lint tables exist even if the test DB was created before the
# lint-subsystem schema additions. Runs once per module before any test.
@pytest.fixture(autouse=True)
async def _ensure_lint_schema():
    from memory import db
    sql_path = Path(__file__).resolve().parent.parent / "memory" / "schema.sql"
    schema = sql_path.read_text()
    # Extract just the lint-related DDL (idempotent anyway) — easier: apply full schema.
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(schema)
            # Wipe lint tables between tests
            await cur.execute("TRUNCATE memory_lint_findings RESTART IDENTITY")
            await cur.execute("TRUNCATE memory_lint_runs RESTART IDENTITY")
        await conn.commit()
    yield


async def _insert_doc_chunk(content: str, source_path: str, section: str,
                            mtime: int | None = None):
    """Low-level helper that bypasses the chunker to insert a synthetic chunk."""
    from memory import db, embeddings
    from pgvector.psycopg import Vector
    emb = await embeddings.embed_one(content)
    meta = {"mtime": mtime} if mtime is not None else {}
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memories (kind, content, source_path, source_section,"
                " metadata, embedding, stability) "
                "VALUES ('doc_chunk', %s, %s, %s, %s::jsonb, %s, 30) RETURNING id",
                (content, source_path, section, json.dumps(meta), Vector(emb)),
            )
            row = await cur.fetchone()
            await conn.commit()
    return str(row[0])


# ---------------------------------------------------------------------------
# 1. Orphan detection
# ---------------------------------------------------------------------------

async def test_orphan_detection():
    from memory import store, lint

    # Insert an insight with no doc_chunks anywhere.
    ins = await store.insert_insight("xylophone purple quasar zeta whimsical")
    findings = await lint.check_orphan_insights(orphan_threshold=0.4)
    ids = {f.subject_id for f in findings}
    assert ins["id"] in ids
    orphan = next(f for f in findings if f.subject_id == ins["id"])
    assert orphan.kind == "orphan"
    assert orphan.suggestion


# ---------------------------------------------------------------------------
# 2. Redundant detection + dedup on rerun
# ---------------------------------------------------------------------------

async def test_redundant_detection():
    from memory import store, lint

    text_a = "floating license revocation procedure step by step detail"
    text_b = "floating license revocation procedure step by step detail"
    a = await store.insert_insight(text_a)
    b = await store.insert_insight(text_b)

    findings = await lint.check_redundant_insights(min_sim=0.85)
    # Expect at least one finding linking a<->b
    pair = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
    assert any(
        f.subject_id == pair[0] and f.related_id == pair[1]
        for f in findings
    ), f"expected pair {pair} in {[(f.subject_id, f.related_id) for f in findings]}"

    # Persist, then rerun: unique index should prevent duplicate
    from memory.lint import _persist_findings
    n1 = await _persist_findings(findings)
    assert n1 >= 1
    n2 = await _persist_findings(findings)
    assert n2 == 0  # dedup via unique index


# ---------------------------------------------------------------------------
# 3. Stale doc_chunks
# ---------------------------------------------------------------------------

async def test_stale_doc_chunks(tmp_path):
    from memory import lint

    # Build a temp repo layout
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    f = repo / "docs" / "foo.md"
    f.write_text("# Foo\n\nhello world.\n")

    # Chunk mtime is old
    old_mtime = int(time.time()) - 3600  # 1 hour old
    await _insert_doc_chunk(
        content="# Foo\n\nhello world.",
        source_path="docs/foo.md",
        section="foo",
        mtime=old_mtime,
    )
    # File on disk mtime is "now", so > chunk_mtime + 300s
    findings = await lint.check_stale_doc_chunks(repo)
    assert len(findings) == 1
    assert findings[0].kind == "stale"

    # Persist
    from memory.lint import _persist_findings
    n = await _persist_findings(findings)
    assert n == 1

    # Update chunk's metadata.mtime to match file (simulate reindex)
    from memory import db
    file_mtime = int(f.stat().st_mtime)
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories SET metadata = jsonb_set(metadata, '{mtime}', %s::jsonb) "
                "WHERE kind='doc_chunk'",
                (str(file_mtime),),
            )
        await conn.commit()

    # Rerun: should produce no new findings
    findings2 = await lint.check_stale_doc_chunks(repo)
    assert findings2 == []


# ---------------------------------------------------------------------------
# 4. Contradiction skipped without API key
# ---------------------------------------------------------------------------

async def test_contradiction_skipped_without_api_key(monkeypatch):
    from memory import store, lint

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    await store.insert_insight("the license server runs on port 8080 default")
    await store.insert_insight("the license server runs on port 9090 default")

    findings, tokens = await lint.check_contradictions(max_pairs=20)
    assert findings == []
    assert tokens == 0


# ---------------------------------------------------------------------------
# 5. Contradiction with stubbed LLM
# ---------------------------------------------------------------------------

async def test_contradiction_with_stubbed_llm(monkeypatch):
    from memory import store, lint

    monkeypatch.setenv("GOOGLE_API_KEY", "stub-key")

    # Two insights with high but not identical similarity (tokens overlap +
    # differing numbers).
    await store.insert_insight("the license server listens on port 8080 by default always")
    await store.insert_insight("the license server listens on port 9090 by default always")

    async def fake_judge(prompt_a, prompt_b):
        return True, "port numbers differ", 120

    monkeypatch.setattr(lint, "_judge_contradiction", fake_judge)
    # The retry wrapper wraps _judge_contradiction; patch too
    monkeypatch.setattr(lint, "_judge_contradiction_retry", fake_judge)

    findings, tokens = await lint.check_contradictions(
        max_pairs=20, sim_low=0.50, sim_high=0.999,
    )
    assert len(findings) >= 1
    assert findings[0].kind == "contradiction"
    assert tokens > 0


# ---------------------------------------------------------------------------
# 6. auto skips when no writes
# ---------------------------------------------------------------------------

async def test_run_auto_skips_if_no_writes():
    from memory import store, lint

    # Seed some audit activity (5 inserts) and do first run
    for i in range(5):
        await store.insert_insight(f"audit-seed insight number {i} distinctive content")

    r1 = await lint.run_lint(mode="auto", min_writes=5, repo_path=None)
    assert r1["skipped"] is False
    assert r1["mode_used"] in ("cheap", "full")

    # Second run immediately: no new writes → skipped
    r2 = await lint.run_lint(mode="auto", min_writes=5, repo_path=None)
    assert r2["skipped"] is True
    assert r2["mode_used"] == "skipped"
    assert r2["findings_count"] == 0


# ---------------------------------------------------------------------------
# 7. auto does cheap not full if recent full
# ---------------------------------------------------------------------------

async def test_run_auto_does_cheap_not_full_if_recent_full(monkeypatch):
    from memory import db, lint, store

    # Simulate a completed 'full' run 1 hour ago
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memory_lint_runs (started_at, finished_at, mode) "
                "VALUES (now() - interval '1 hour', now() - interval '1 hour', 'full')"
            )
        await conn.commit()

    # Create some writes so auto doesn't skip
    for i in range(6):
        await store.insert_insight(f"cheap-test insight {i} xyz unique content")

    # Monkeypatch contradictions to error if called
    called = {"n": 0}

    async def fake_check(*args, **kwargs):
        called["n"] += 1
        return [], 0

    monkeypatch.setattr(lint, "check_contradictions", fake_check)

    r = await lint.run_lint(mode="auto", min_writes=5,
                            full_interval_hours=168, repo_path=None)
    assert r["mode_used"] == "cheap"
    assert called["n"] == 0, "contradiction check should not run in cheap mode"


# ---------------------------------------------------------------------------
# 8. resolve is idempotent
# ---------------------------------------------------------------------------

async def test_lint_resolve_idempotent():
    from memory import db
    import tools.memory as mem_tools

    registered: dict = {}

    class _StubMCP:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    mem_tools.register(_StubMCP())

    # Insert a finding directly
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memory_lint_findings (kind, summary) "
                "VALUES ('orphan', 'test finding') RETURNING id"
            )
            row = await cur.fetchone()
            await conn.commit()
    fid = int(row[0])

    lint_resolve = registered["lint_resolve"]

    r1 = await lint_resolve(fid, note="first")
    assert r1["id"] == fid
    assert r1["resolved_at"] is not None
    assert r1["resolution_note"] == "first"

    r2 = await lint_resolve(fid, note="second")
    assert r2["id"] == fid
    # resolved_at preserved; note preserved from first call
    assert r2["resolution_note"] == "first"
