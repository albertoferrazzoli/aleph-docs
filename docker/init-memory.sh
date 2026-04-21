#!/usr/bin/env bash
# Render mcp/memory/schema.sql with vector(EMBED_DIM) then load it.
#
# Postgres runs every file in /docker-entrypoint-initdb.d/ once on
# first startup (when the data dir is empty). Because `vector(N)` is
# fixed at CREATE TABLE time, the dim must be chosen BEFORE the rows
# exist — and it has to match the active EMBED_BACKEND:
#
#   gemini-001, gemini-2-preview        → 1536   (default)
#   local (Ollama bge-m3)               → 1024
#   local (Ollama nomic-embed-text)     → 768
#
# We accept `EMBED_DIM` from the environment (propagated via the
# docker-compose db service) and sed-substitute the hardcoded 1536 in
# the schema before piping to psql. If the var isn't set we keep the
# 1536 default for backwards compatibility with the Gemini-era setup.
#
# When switching backends you MUST `docker compose down -v` to wipe
# db_data before `up` — this init script only runs on a blank data
# directory. Matches the "Switching between modes" note in README.

set -euo pipefail

: "${EMBED_DIM:=1536}"
: "${POSTGRES_USER:?POSTGRES_USER must be set}"
: "${POSTGRES_DB:?POSTGRES_DB must be set}"

SCHEMA_SRC=/var/lib/aleph/schema-memory.sql

if [[ ! -f "$SCHEMA_SRC" ]]; then
    echo "[init-memory] schema template not found at $SCHEMA_SRC" >&2
    exit 1
fi

echo "[init-memory] loading mcp memory schema with vector(${EMBED_DIM})"

sed -E "s/vector\\(1536\\)/vector(${EMBED_DIM})/g" "$SCHEMA_SRC" \
    | psql -v ON_ERROR_STOP=1 \
           --username "$POSTGRES_USER" \
           --dbname "$POSTGRES_DB"
