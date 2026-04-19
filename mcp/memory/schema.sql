-- Semantic memory schema for aleph-docs-mcp MCP
-- Idempotent DDL: safe to run multiple times.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE memory_kind AS ENUM ('doc_chunk', 'interaction', 'insight');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            memory_kind NOT NULL,
    content         TEXT NOT NULL,
    source_path     TEXT,
    source_section  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding       vector(1536) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_access_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count    INT NOT NULL DEFAULT 0,
    stability       DOUBLE PRECISION NOT NULL DEFAULT 7.0
);

CREATE INDEX IF NOT EXISTS memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS memories_kind_idx ON memories(kind);
CREATE INDEX IF NOT EXISTS memories_source_idx ON memories(source_path);

-- Multimodal columns (PRD_MULTIMODAL §5.2). Idempotent.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS media_ref    TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS media_type   TEXT;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS preview_b64  TEXT;
CREATE INDEX IF NOT EXISTS memories_media_type_idx ON memories(media_type);

-- Extend memory_kind ENUM with multimodal values (idempotent).
-- ADD VALUE IF NOT EXISTS requires Postgres 9.6+ and cannot run inside a
-- transaction block, but psql executes each statement individually so this
-- works when the schema is piped through `psql -f`.
ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'image';
ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'video_scene';
ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'audio_clip';
ALTER TYPE memory_kind ADD VALUE IF NOT EXISTS 'pdf_page';

CREATE UNIQUE INDEX IF NOT EXISTS memories_doc_chunk_uniq
    ON memories(source_path, source_section)
    WHERE kind = 'doc_chunk';

CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Audit trail for memory writes (append-only).
-- See memory/audit.py for semantics. Snapshot-style: content/kind are captured
-- at write time so delete events retain a trail after the source row is gone.
CREATE TABLE IF NOT EXISTS memory_audit (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  op         TEXT NOT NULL,           -- insert | update | delete | reinforce | access
  subject_id UUID,                    -- memories.id (NULL for bulk ops)
  actor      TEXT,                    -- free-form: 'mcp:remember', 'aleph:ui', 'indexer', 'store:search'
  kind       TEXT,                    -- snapshot of memories.kind at the moment (for forget events)
  content    TEXT,                    -- snapshot of content (truncated 1000 chars)
  metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS memory_audit_ts_idx ON memory_audit(ts DESC);
CREATE INDEX IF NOT EXISTS memory_audit_subject_idx ON memory_audit(subject_id);
CREATE INDEX IF NOT EXISTS memory_audit_op_idx ON memory_audit(op);

-- ---------------------------------------------------------------------------
-- Memory lint findings + runs (Feature C)
-- See memory/lint.py for semantics.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_lint_findings (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind       TEXT NOT NULL,        -- 'orphan' | 'redundant' | 'contradiction' | 'stale'
  severity   TEXT NOT NULL DEFAULT 'warning',
  subject_id UUID,
  related_id UUID,
  summary    TEXT NOT NULL,
  suggestion TEXT,
  resolved_at TIMESTAMPTZ,
  resolution_note TEXT,
  metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS memory_lint_findings_unresolved_idx
    ON memory_lint_findings(ts DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS memory_lint_findings_kind_idx
    ON memory_lint_findings(kind);
CREATE INDEX IF NOT EXISTS memory_lint_findings_subject_idx
    ON memory_lint_findings(subject_id);
-- Idempotent dedup: avoid re-inserting the same (kind, subject, related) pair
-- when it's still unresolved.
CREATE UNIQUE INDEX IF NOT EXISTS memory_lint_findings_open_unique
    ON memory_lint_findings(kind, subject_id, COALESCE(related_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS memory_lint_runs (
  id             BIGSERIAL PRIMARY KEY,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  mode           TEXT NOT NULL,       -- 'cheap' | 'full' | 'manual' | 'skipped'
  new_findings   INT DEFAULT 0,
  llm_pairs_evaluated INT DEFAULT 0,
  tokens_used    INT DEFAULT 0,
  cost_estimate_usd NUMERIC(8, 5) DEFAULT 0,
  error          TEXT,
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS memory_lint_runs_ts_idx ON memory_lint_runs(started_at DESC);
