-- Aleph schema additions. Idempotent.

CREATE TABLE IF NOT EXISTS graph_snapshot (
  id SERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  version BIGINT NOT NULL UNIQUE,
  payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS graph_snapshot_version_idx
  ON graph_snapshot(version DESC);
