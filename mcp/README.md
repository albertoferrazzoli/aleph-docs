# Aleph Docs MCP Server

MCP server that exposes the product documentation
(`github.com/<DOCS_REPO_SLUG>`) to Claude Desktop for answering
customer support emails.

## Quick start (local)

```bash
cd mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # edit MCP_API_KEY
python indexer.py --rebuild    # clones repo + builds FTS index
python server.py               # listens on 127.0.0.1:8001
```

## Tools (16)

| Category | Tool |
|---|---|
| Search | `search_docs`, `search_code_examples`, `find_related` |
| Navigation | `list_sections`, `get_page_tree`, `list_pages` |
| Content | `get_page`, `get_page_section`, `get_table_of_contents`, `get_code_blocks` |
| Lookup | `find_command_line_option`, `find_config_option`, `find_error_message`, `find_api_endpoint` |
| Meta | `get_doc_stats`, `get_changelog` |

## Doc patch workflow

The semantic-memory tool `propose_doc_patch` turns accumulated insights into a
ready-to-review git commit on the canonical docs repo (`<DOCS_REPO_NAME>`).

1. You call `propose_doc_patch(topic="...")` from Claude.
2. The server picks the best target file under `content/`, builds a
   `## Notes from support (auto-suggested)` block (override via
   `DOC_PATCH_HEADING` env to match the docs' language), creates a local branch
   `docs/mcp-<slug>-<YYYYMMDD-HHMM>` in the clone, inserts the block after the
   relevant H2 (or at EOF), and commits. **It does not push.**
3. Review on the VM:

   ```bash
   cd /opt/mcp/repo
   git diff main...docs/mcp-<slug>-<stamp>
   ```

4. When you're happy, push manually:

   ```bash
   git push origin docs/mcp-<slug>-<stamp>
   ```

   This requires a GitHub token with write scope on the docs repo — see
   `.env.example` (`DOCS_REPO_TOKEN`). Auto-push / PR-open is deliberately
   punted to v2.

Pass `dry_run=True` to get the plan without touching the repo. If the working
tree is dirty or the target file is missing, the tool refuses with
`status="error"`.

## Deploy to production

```bash
./deploy-aleph-docs-mcp.sh
```

Deploys to the GCP VM `<VM_NAME>` under `/opt/mcp/`
as systemd service `aleph-docs-mcp` on port 8001 (localhost only).
A systemd timer runs `python indexer.py --update` hourly.

Exposed via Apache at:
- `https://example.com/mcp` (Streamable HTTP)
- `https://example.com/mcp/sse` (SSE)

## Claude Desktop connection

```json
{
  "mcpServers": {
    "aleph-docs-mcp": {
      "type": "url",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_API_KEY" }
    }
  }
}
```

## Index refresh

- On service start: `git pull` + incremental update.
- Every hour: systemd timer triggers `python indexer.py --update`.
- Manual rebuild:
  ```bash
  sudo systemctl stop aleph-docs-mcp
  sudo -u www-data /opt/mcp/.venv/bin/python \
       /opt/mcp/indexer.py --rebuild
  sudo systemctl start aleph-docs-mcp
  ```

## Semantic Memory (pgvector)

Optional PostgreSQL + pgvector layer adds vector search over docs, reinforced
interactions (feedback-weighted memory), and doc update suggestions mined from
recurring support conversations. The feature is opt-in via `MEMORY_ENABLED=true`;
when disabled (or when Postgres is unreachable) the server transparently falls
back to pure SQLite FTS and all existing tools keep working unchanged.

### Local quick start (macOS / Homebrew)

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
createdb aleph_memory
psql aleph_memory -c "CREATE EXTENSION vector"
psql aleph_memory -f memory/schema.sql
export PG_DSN=postgresql:///aleph_memory
export GOOGLE_API_KEY=...
export MEMORY_ENABLED=true
./.venv/bin/python -m memory.bootstrap     # initial embedding of all docs
./.venv/bin/python server.py
curl -s http://127.0.0.1:8001/health | jq
```

### Production (VM)

`deploy-aleph-docs-mcp.sh` installs Postgres 16 and `postgresql-16-pgvector`,
creates the `aleph_memory` database, applies `memory/schema.sql` and runs
the initial `memory.bootstrap` embedding pass automatically. Secrets
(`PG_DSN`, `GOOGLE_API_KEY`, `MEMORY_ENABLED`) live in the `.env` file on the
VM and are never committed.

### Choosing a backend

Embeddings go through a pluggable registry under `mcp/memory/embedders/`.
Pick one with `EMBED_BACKEND`:

- `gemini-001` *(default)* — text-only, 1536-dim, cheapest cloud.
- `gemini-2-preview` — multimodal (text + image + video + audio + pdf),
  3072-dim native with MRL truncation.
- `local` — offline via Ollama (`bge-m3` by default, 1024-dim). Requires
  `ollama pull bge-m3` and `EMBED_DIM=1024`.

Backends produce incompatible vector spaces, so switching requires a
full re-bootstrap (`CONFIRM_REEMBED=yes python -m memory.bootstrap
--reembed-all`). See `PRD_MULTIMODAL.md §5.0` for the full trade-off
matrix.

### Tools exposed

- `semantic_search(query, kind?, limit, min_score)` — unified vector search across docs, insights and interactions.
- `remember(content, context, source_path?, tags?)` — save an explicit insight for later recall.
- `recall(query, limit)` — alias for retrieving insights + interactions relevant to a query.
- `suggest_doc_update(topic, top_k)` — markdown patch proposal for canonical docs based on recurring interactions.
- `forget(memory_id)` — explicit deletion of a stored memory.

### Troubleshooting

- `CREATE EXTENSION vector` fails → install `pgvector` for the Postgres version in use (`brew install pgvector`, or on Debian `apt install postgresql-16-pgvector`).
- `/health` returns `memory_count: null` → check `PG_DSN`, ensure Postgres is running, and inspect `journalctl -u aleph-docs-mcp | grep "[memory]"`.
- Rate-limit / quota errors from Gemini → retry logic already backs off 3x; set `MEMORY_ENABLED=false` temporarily to unblock and resume later.
- Re-embed all docs after model change: `python -m memory.bootstrap --reembed-all`.

## Memory lint

The lint subsystem periodically scans `memories` for quality issues and
persists findings to `memory_lint_findings`. Runs are recorded in
`memory_lint_runs` with tokens/cost telemetry.

### Checks

- **orphan**        — insight whose top-5 nearest neighbors contain no `doc_chunk`
                      above `LINT_ORPHAN_THRESHOLD` (default 0.4). Suggests
                      linking to canonical docs or forgetting.
- **redundant**     — pair of insights with cosine similarity > `LINT_REDUNDANT_SIM`
                      (default 0.85). Suggests merging / deleting the
                      less-accessed one.
- **stale**         — `doc_chunk` whose file on disk is more than 300s newer
                      than the stored `metadata.mtime`. Suggests `indexer --update`.
- **contradiction** (LLM) — pair of insights with cosine similarity in
                      `[LINT_SIM_LOW, LINT_SIM_HIGH]` (default 0.70 .. 0.95)
                      judged by `gemini-2.5-flash` to assert incompatible facts.

### Cost envelope

- gemini-2.5-flash: $0.075/1M input, $0.30/1M output.
- Per pair: ~600 input + 50 output tokens ≈ **$0.00006**.
- Cap: `LINT_MAX_PAIRS=20` → **$0.0012 per full run**.
- Full runs default weekly → **~$0.06/year** worst case; cheap runs are free.

### Scheduling

A systemd timer (`aleph-docs-mcp-lint.timer`) fires every 6h in `auto` mode.
The orchestrator:

- skips if fewer than `LINT_MIN_WRITES` audit events since the last successful run;
- runs only cheap checks if a `full` ran within `LINT_FULL_INTERVAL_HOURS`;
- otherwise runs a `full` pass (cheap + LLM contradiction check).

### Env vars

```
LINT_MODE=auto                 # default mode for the CLI
LINT_MIN_WRITES=5              # audit-event threshold to do any work
LINT_FULL_INTERVAL_HOURS=168   # at most one 'full' per week
LINT_MAX_PAIRS=20              # cap on LLM pair evaluations
LINT_SIM_LOW=0.70              # contradiction lower bound
LINT_SIM_HIGH=0.95             # contradiction upper bound
LINT_REDUNDANT_SIM=0.85        # redundant-insight threshold
LINT_ORPHAN_THRESHOLD=0.4      # doc_chunk similarity floor for grounding
LINT_LLM_MODEL=gemini-2.5-flash
```

### Manual usage

```bash
# Trigger locally
./.venv/bin/python -m memory.lint_cli --mode manual   # forces full, no skip
./.venv/bin/python -m memory.lint_cli --mode cheap    # no LLM calls

# On the VM
sudo systemctl start aleph-docs-mcp-lint.service
journalctl -u aleph-docs-mcp-lint -n 50
```

### Inspect findings via MCP

- `lint_run(mode)` — trigger a run on demand.
- `lint_findings(kind?, include_resolved?, limit?)` — list open findings.
- `lint_resolve(finding_id, note?)` — acknowledge a finding.

Graceful degradation: missing `GOOGLE_API_KEY` simply skips the contradiction
check with a warning — all cheap checks still run.
