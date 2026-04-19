"""Semantic memory lint subsystem (Feature C).

Periodically scans the `memories` table for quality issues:
  - orphan       : insights not grounded in any canonical doc_chunk
  - redundant    : near-duplicate insights (cosine sim > threshold)
  - contradiction: similar-but-not-identical insight pairs judged by LLM
  - stale        : doc_chunks whose source file on disk is newer than the chunk

Cost-controlled: cheap checks are pure SQL+FS. Contradiction checks use a
capped number of gemini-2.5-flash calls per run. Missing GOOGLE_API_KEY
degrades gracefully (skip contradiction, don't fail).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import audit, db

log = logging.getLogger("memory.lint")


# ---------------------------------------------------------------------------
# Tunables (env-overridable; defaults match task spec)
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r, using default %s", key, raw, default)
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r, using default %s", key, raw, default)
        return default


# gemini-2.5-flash pricing (USD per 1M tokens, as of 2026)
_FLASH_INPUT_USD_PER_M = 0.075
_FLASH_OUTPUT_USD_PER_M = 0.30
# Conservative blended rate used for cost_estimate (output-dominant worst case).
_FLASH_BLENDED_USD_PER_M = 0.30


@dataclass
class Finding:
    kind: str              # 'orphan'|'redundant'|'contradiction'|'stale'
    severity: str          # 'info'|'warning'|'error'
    subject_id: str
    related_id: Optional[str]
    summary: str
    suggestion: Optional[str]
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cheap checks
# ---------------------------------------------------------------------------

async def check_orphan_insights(
    orphan_threshold: float | None = None,
) -> list[Finding]:
    """For each insight, examine top-5 neighbors via pgvector.

    If none are doc_chunk with similarity > threshold, mark as orphan.
    """
    threshold = (
        _env_float("LINT_ORPHAN_THRESHOLD", 0.4)
        if orphan_threshold is None
        else orphan_threshold
    )
    findings: list[Finding] = []

    sql = """
    WITH insights AS (
        SELECT id, content, embedding FROM memories WHERE kind='insight'
    ),
    top_neighbors AS (
        SELECT
            i.id AS insight_id,
            i.content AS insight_content,
            m.id AS neighbor_id,
            m.kind AS neighbor_kind,
            1 - (m.embedding <=> i.embedding) AS sim,
            ROW_NUMBER() OVER (
                PARTITION BY i.id
                ORDER BY m.embedding <=> i.embedding
            ) AS rn
        FROM insights i
        LEFT JOIN memories m
          ON m.id <> i.id
    )
    SELECT i.id, i.content,
           (
             SELECT MAX(sim) FROM top_neighbors tn
             WHERE tn.insight_id = i.id
               AND tn.neighbor_kind = 'doc_chunk'
               AND tn.rn <= 5
           ) AS best_doc_sim
    FROM insights i
    """

    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
    except db.MemoryDisabled:
        return []

    for iid, content, best_sim in rows:
        if best_sim is None or float(best_sim) <= threshold:
            snippet = (content or "").replace("\n", " ").strip()[:160]
            findings.append(Finding(
                kind="orphan",
                severity="warning",
                subject_id=str(iid),
                related_id=None,
                summary=f"Insight not grounded in docs: {snippet}",
                suggestion="link to a doc_chunk or consider forgetting",
                metadata={"best_doc_sim": float(best_sim) if best_sim is not None else None,
                          "threshold": threshold},
            ))
    log.info("[lint] orphan check: %d findings", len(findings))
    return findings


async def check_redundant_insights(min_sim: float | None = None) -> list[Finding]:
    """Emit one finding per near-duplicate insight pair (a.id < b.id)."""
    threshold = (
        _env_float("LINT_REDUNDANT_SIM", 0.85) if min_sim is None else min_sim
    )
    findings: list[Finding] = []

    sql = """
    SELECT a.id, b.id,
           1 - (a.embedding <=> b.embedding) AS sim,
           a.content, b.content,
           a.access_count, b.access_count
    FROM memories a
    JOIN memories b
      ON a.kind='insight' AND b.kind='insight' AND a.id < b.id
    WHERE 1 - (a.embedding <=> b.embedding) > %s
    ORDER BY sim DESC
    LIMIT 200
    """

    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (threshold,))
                rows = await cur.fetchall()
    except db.MemoryDisabled:
        return []

    for aid, bid, sim, a_content, b_content, a_ac, b_ac in rows:
        a_snip = (a_content or "").replace("\n", " ").strip()[:120]
        b_snip = (b_content or "").replace("\n", " ").strip()[:120]
        findings.append(Finding(
            kind="redundant",
            severity="warning",
            subject_id=str(aid),
            related_id=str(bid),
            summary=f"Near-duplicate insights (sim={float(sim):.2f}): A={a_snip!r} / B={b_snip!r}",
            suggestion="merge or delete the less-accessed one",
            metadata={
                "similarity": float(sim),
                "a_access_count": int(a_ac or 0),
                "b_access_count": int(b_ac or 0),
            },
        ))
    log.info("[lint] redundant check: %d findings", len(findings))
    return findings


async def check_stale_doc_chunks(repo_path: Path | None) -> list[Finding]:
    """Compare metadata.mtime vs file's mtime on disk.

    Skip the whole check if repo_path is missing; skip individual chunks whose
    source file no longer exists (those get cleaned up by the indexer delete
    path, not by lint).
    """
    findings: list[Finding] = []
    if repo_path is None:
        log.info("[lint] stale check skipped: no repo_path")
        return findings
    if not repo_path.exists():
        log.info("[lint] stale check skipped: repo_path %s does not exist", repo_path)
        return findings

    sql = """
    SELECT id, source_path, source_section, metadata
    FROM memories WHERE kind='doc_chunk'
    """
    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
    except db.MemoryDisabled:
        return []

    grace_seconds = 300

    for mid, source_path, source_section, metadata in rows:
        meta = metadata or {}
        chunk_mtime = meta.get("mtime")
        if chunk_mtime is None or not source_path:
            continue
        f = repo_path / source_path
        if not f.exists():
            continue
        try:
            file_mtime = int(f.stat().st_mtime)
        except OSError:
            continue
        try:
            chunk_mtime_i = int(chunk_mtime)
        except (TypeError, ValueError):
            continue
        if file_mtime > chunk_mtime_i + grace_seconds:
            findings.append(Finding(
                kind="stale",
                severity="info",
                subject_id=str(mid),
                related_id=None,
                summary=f"doc_chunk stale: {source_path}#{source_section or ''} "
                        f"(file {file_mtime - chunk_mtime_i}s newer than chunk)",
                suggestion="run indexer --update",
                metadata={
                    "source_path": source_path,
                    "source_section": source_section,
                    "chunk_mtime": chunk_mtime_i,
                    "file_mtime": file_mtime,
                },
            ))
    log.info("[lint] stale check: %d findings", len(findings))
    return findings


# ---------------------------------------------------------------------------
# Contradiction check (LLM)
# ---------------------------------------------------------------------------

# Indirection point so tests can monkeypatch LLM calls without importing
# google-genai.
async def _judge_contradiction(prompt_a: str, prompt_b: str) -> tuple[bool, str, int]:
    """Return (contradicts, reason, tokens_used_estimate).

    Raises on unexpected API errors (caller wraps). Returns (False, "no-api-key",
    0) if GOOGLE_API_KEY missing.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return False, "no-api-key", 0

    from google import genai  # type: ignore
    from google.genai import types as genai_types  # type: ignore

    client = genai.Client(api_key=api_key)
    model = os.environ.get("LINT_LLM_MODEL", "gemini-2.5-flash")

    system = (
        "You are a quality reviewer. Return JSON "
        '{"contradicts": bool, "reason": "<20 words>"}.'
    )
    user = f"A: {prompt_a}\n\nB: {prompt_b}"

    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=0.0,
        max_output_tokens=150,
    )

    resp = await client.aio.models.generate_content(
        model=model,
        contents=user,
        config=config,
    )
    text = (getattr(resp, "text", "") or "").strip()
    try:
        data = json.loads(text)
        contradicts = bool(data.get("contradicts"))
        reason = str(data.get("reason", ""))[:200]
    except (ValueError, TypeError):
        contradicts = False
        reason = f"unparseable: {text[:80]}"

    tokens = (len(prompt_a) + len(prompt_b) + 50) // 4
    return contradicts, reason, tokens


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def _judge_contradiction_retry(prompt_a: str, prompt_b: str):
    return await _judge_contradiction(prompt_a, prompt_b)


async def check_contradictions(
    max_pairs: int | None = None,
    sim_low: float | None = None,
    sim_high: float | None = None,
) -> tuple[list[Finding], int]:
    """Expensive check. Returns (findings, tokens_used).

    Gracefully returns ([], 0) if GOOGLE_API_KEY is missing.
    """
    cap = _env_int("LINT_MAX_PAIRS", 20) if max_pairs is None else max_pairs
    low = _env_float("LINT_SIM_LOW", 0.70) if sim_low is None else sim_low
    high = _env_float("LINT_SIM_HIGH", 0.95) if sim_high is None else sim_high

    if not os.environ.get("GOOGLE_API_KEY", "").strip():
        log.warning("[lint] contradiction check skipped: GOOGLE_API_KEY missing")
        return [], 0

    sql = """
    SELECT a.id, b.id,
           1 - (a.embedding <=> b.embedding) AS sim,
           a.content, b.content,
           a.access_count, b.access_count
    FROM memories a
    JOIN memories b
      ON a.kind='insight' AND b.kind='insight' AND a.id < b.id
    WHERE 1 - (a.embedding <=> b.embedding) BETWEEN %s AND %s
    ORDER BY (1 - (a.embedding <=> b.embedding)) *
             (COALESCE(a.access_count, 0) + COALESCE(b.access_count, 0) + 1) DESC
    LIMIT %s
    """
    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (low, high, cap))
                rows = await cur.fetchall()
    except db.MemoryDisabled:
        return [], 0

    findings: list[Finding] = []
    total_tokens = 0

    for aid, bid, sim, a_content, b_content, _a_ac, _b_ac in rows:
        try:
            contradicts, reason, tokens = await _judge_contradiction_retry(
                a_content or "", b_content or ""
            )
        except Exception as e:
            log.warning("[lint] contradiction judge failed on pair (%s,%s): %s", aid, bid, e)
            continue
        total_tokens += int(tokens)
        if contradicts:
            a_snip = (a_content or "").replace("\n", " ").strip()[:100]
            b_snip = (b_content or "").replace("\n", " ").strip()[:100]
            findings.append(Finding(
                kind="contradiction",
                severity="error",
                subject_id=str(aid),
                related_id=str(bid),
                summary=f"Possible contradiction (sim={float(sim):.2f}): {reason}",
                suggestion="review both insights; keep one or reconcile",
                metadata={
                    "similarity": float(sim),
                    "reason": reason,
                    "a_snippet": a_snip,
                    "b_snippet": b_snip,
                },
            ))
    log.info("[lint] contradiction check: %d findings, %d tokens", len(findings), total_tokens)
    return findings, total_tokens


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def _persist_findings(findings: list[Finding]) -> int:
    """Insert findings, skipping duplicates on the open-unique partial index."""
    if not findings:
        return 0
    new_count = 0
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            for f in findings:
                try:
                    await cur.execute(
                        """
                        INSERT INTO memory_lint_findings
                          (kind, severity, subject_id, related_id, summary,
                           suggestion, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (
                            f.kind, f.severity,
                            f.subject_id, f.related_id,
                            f.summary, f.suggestion,
                            json.dumps(f.metadata or {}),
                        ),
                    )
                    row = await cur.fetchone()
                    if row:
                        new_count += 1
                except Exception as e:
                    log.warning("[lint] persist finding failed (%s/%s): %s",
                                f.kind, f.subject_id, e)
        await conn.commit()
    return new_count


async def _insert_run_row(mode: str) -> int:
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memory_lint_runs (mode) VALUES (%s) RETURNING id",
                (mode,),
            )
            row = await cur.fetchone()
            await conn.commit()
    return int(row[0])


async def _finish_run_row(
    run_id: int, *,
    mode: str,
    new_findings: int,
    llm_pairs: int,
    tokens: int,
    cost_usd: float,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE memory_lint_runs
                SET finished_at = now(),
                    mode = %s,
                    new_findings = %s,
                    llm_pairs_evaluated = %s,
                    tokens_used = %s,
                    cost_estimate_usd = %s,
                    error = %s,
                    metadata = %s::jsonb
                WHERE id = %s
                """,
                (mode, new_findings, llm_pairs, tokens,
                 round(cost_usd, 5), error,
                 json.dumps(metadata or {}), run_id),
            )
            await conn.commit()


async def _count_audit_since(last_run_ts) -> int:
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            if last_run_ts is None:
                await cur.execute(
                    "SELECT COUNT(*) FROM memory_audit WHERE op IN ('insert','update','delete')"
                )
            else:
                await cur.execute(
                    "SELECT COUNT(*) FROM memory_audit "
                    "WHERE ts > %s AND op IN ('insert','update','delete')",
                    (last_run_ts,),
                )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _last_run_info() -> dict:
    """Return {last_any, last_full, last_success_ts} or Nones."""
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT started_at FROM memory_lint_runs "
                "WHERE finished_at IS NOT NULL AND (error IS NULL OR error='') "
                "  AND mode <> 'skipped' "
                "ORDER BY started_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            last_success = row[0] if row else None

            await cur.execute(
                "SELECT started_at FROM memory_lint_runs "
                "WHERE finished_at IS NOT NULL AND (error IS NULL OR error='') "
                "  AND mode = 'full' "
                "ORDER BY started_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            last_full = row[0] if row else None
    return {"last_success": last_success, "last_full": last_full}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_lint(
    mode: str = "auto",
    min_writes: int | None = None,
    full_interval_hours: int | None = None,
    max_pairs: int | None = None,
    repo_path: Path | None = None,
) -> dict:
    """Orchestrate a lint run. See module docstring for modes."""
    min_writes = (
        _env_int("LINT_MIN_WRITES", 5) if min_writes is None else int(min_writes)
    )
    full_interval_hours = (
        _env_int("LINT_FULL_INTERVAL_HOURS", 168)
        if full_interval_hours is None
        else int(full_interval_hours)
    )
    max_pairs = (
        _env_int("LINT_MAX_PAIRS", 20) if max_pairs is None else int(max_pairs)
    )

    if mode not in ("auto", "cheap", "full", "manual"):
        raise ValueError(f"invalid mode: {mode}")

    if not db.is_enabled():
        return {
            "run_id": None, "mode_used": mode, "findings_count": 0,
            "tokens_used": 0, "cost_usd": 0.0, "skipped": True,
            "duration_seconds": 0.0,
            "error": "memory disabled",
        }

    t_start = time.monotonic()
    run_id = await _insert_run_row(mode)
    metadata: dict[str, Any] = {"input_mode": mode}

    try:
        # 1. Decide mode_used
        last = await _last_run_info()
        mode_used = mode
        skipped_reason: str | None = None

        if mode == "auto":
            since_ts = last["last_success"]
            writes = await _count_audit_since(since_ts)
            metadata["audit_writes_since_last"] = writes
            if writes < min_writes:
                mode_used = "skipped"
                skipped_reason = f"writes={writes}<{min_writes}"
            else:
                # Downgrade to cheap if a full ran recently
                last_full = last["last_full"]
                if last_full is not None:
                    async with db.get_conn() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT EXTRACT(EPOCH FROM (now() - %s))/3600.0",
                                (last_full,),
                            )
                            row = await cur.fetchone()
                    hours_since_full = float(row[0]) if row else 9999.0
                    metadata["hours_since_full"] = round(hours_since_full, 2)
                    mode_used = "cheap" if hours_since_full < full_interval_hours else "full"
                else:
                    mode_used = "full"

        # 2. Execute
        all_findings: list[Finding] = []
        tokens = 0
        llm_pairs = 0

        if mode_used != "skipped":
            all_findings += await check_orphan_insights()
            all_findings += await check_redundant_insights()
            all_findings += await check_stale_doc_chunks(repo_path)

            if mode_used in ("full", "manual"):
                contradict_findings, tokens = await check_contradictions(
                    max_pairs=max_pairs
                )
                llm_pairs = min(max_pairs, len(contradict_findings)) \
                    if contradict_findings else 0
                # A more accurate pair count: we don't always know from findings;
                # keep tokens as the cost signal.
                all_findings += contradict_findings

        new_count = await _persist_findings(all_findings)

        # 3. Cost
        cost_usd = tokens * _FLASH_BLENDED_USD_PER_M / 1_000_000.0

        await _finish_run_row(
            run_id,
            mode=mode_used,
            new_findings=new_count,
            llm_pairs=llm_pairs,
            tokens=tokens,
            cost_usd=cost_usd,
            error=None,
            metadata={**metadata, "skipped_reason": skipped_reason}
                if skipped_reason else metadata,
        )

        duration = time.monotonic() - t_start
        result = {
            "run_id": run_id,
            "mode_used": mode_used,
            "findings_count": new_count,
            "tokens_used": tokens,
            "cost_usd": round(cost_usd, 6),
            "skipped": mode_used == "skipped",
            "duration_seconds": round(duration, 3),
        }

        try:
            await audit.record(
                "lint",
                subject_id=None,
                actor="lint:run",
                kind=None,
                content=None,
                metadata={"run_id": run_id, "findings_count": new_count,
                          "mode_used": mode_used, "tokens_used": tokens},
            )
        except Exception as e:  # pragma: no cover - audit is best-effort
            log.warning("[lint] audit record failed: %s", e)

        return result

    except Exception as e:
        duration = time.monotonic() - t_start
        log.exception("[lint] run failed")
        try:
            await _finish_run_row(
                run_id, mode=mode, new_findings=0, llm_pairs=0,
                tokens=0, cost_usd=0.0, error=str(e)[:500],
                metadata=metadata,
            )
        except Exception:
            pass
        return {
            "run_id": run_id, "mode_used": mode, "findings_count": 0,
            "tokens_used": 0, "cost_usd": 0.0, "skipped": False,
            "duration_seconds": round(duration, 3),
            "error": str(e),
        }
