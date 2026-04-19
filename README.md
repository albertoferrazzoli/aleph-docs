# Aleph Docs

**A reusable template for a documentation-aware LLM knowledge system.**

This repository provides everything you need to stand up, for any product,
a stack that combines:

1. A **MCP server** that indexes a Markdown documentation repo and exposes
   lexical search (SQLite FTS5) + semantic search (PostgreSQL + pgvector)
   + Gemini embeddings + Ebbinghaus forgetting curve + audit trail + lint.
2. **Aleph**, a 3D web viewer for the semantic memory graph (Vite + React +
   Three.js), with live updates via Postgres LISTEN/NOTIFY.

Together they implement an **operational knowledge system** that competes
feature-for-feature with the LLM-Wiki pattern (e.g. Karpathy's design) while
being cheaper at scale thanks to pgvector retrieval, auto-reinforcement and
forgetting-curve decay.

> **Starting a new deployment?** Follow [`SETUP.md`](SETUP.md) — it's
> written as an AI-coder runbook, step by step, from a blank VM to a
> working system.

---

## What you get

| Capability | Implementation |
|---|---|
| Lexical search over docs | SQLite FTS5, hourly re-indexed from a GitHub Markdown repo |
| Semantic search (docs + insights + interactions) | pgvector HNSW with cosine + Ebbinghaus decay |
| Auto-reinforcement | Every hit bumps `stability × 1.7`, `access_count += 1` |
| Manual knowledge capture | `remember(content, context)` MCP tool |
| Manual pruning | `forget(memory_id)` with audit snapshot preserved |
| Audit trail | `memory_audit` table + `audit_history` MCP tool |
| Doc-patch proposals | `suggest_doc_update`, `propose_doc_patch(open_pr=true)` opens PRs on the docs repo |
| Quality linting | `lint_run` with 4 checks (orphan, redundant, stale, contradiction) + cost-capped LLM judge |
| Live 3D viewer | UMAP + HDBSCAN projection, SSE patches, right-panel with audit history |
| Multi-layer auth | Apache Basic Auth on perimeter + `X-Aleph-Key` on write endpoints |
| Idempotent deploy | `deploy-mcp.sh` and `deploy-aleph.sh` safe to re-run |

---

## Repository layout

```
aleph-docs/
├── README.md                 # this file
├── SETUP.md                  # step-by-step bring-up runbook
├── ARCHITECTURE.md           # design notes + diagrams
├── .env.example              # top-level env template (copy to .env before deploy)
│
├── mcp/                      # the MCP server
│   ├── server.py             # FastMCP app + /health + /mcp + /sse
│   ├── indexer.py            # git clone + markdown → SQLite FTS5
│   ├── auth.py               # bearer-token middleware
│   ├── helpers.py
│   ├── requirements.txt
│   ├── memory/               # the semantic memory core
│   │   ├── schema.sql        # all DDL (idempotent)
│   │   ├── db.py             # async psycopg pool + pgvector
│   │   ├── embeddings.py     # Gemini client with tenacity retry
│   │   ├── chunker.py        # H2/H3-aware markdown chunking
│   │   ├── store.py          # CRUD + forgetting-curve reinforcement
│   │   ├── reinforce.py      # @record_interaction decorator
│   │   ├── bootstrap.py      # one-shot: embed all docs
│   │   ├── audit.py          # best-effort write-log
│   │   ├── doc_patch.py      # git branch+commit+PR helpers
│   │   ├── lint.py           # quality checks
│   │   └── lint_cli.py       # CLI + systemd entry
│   ├── tools/                # FastMCP tool modules
│   │   ├── search.py         # search_docs, search_code_examples, find_related
│   │   ├── lookup.py         # find_command_line_option, find_error_message, find_api_endpoint
│   │   ├── navigation.py     # list_sections, get_page_tree, list_pages
│   │   ├── content.py        # get_page, get_page_section, get_code_blocks
│   │   ├── meta.py           # get_doc_stats, get_changelog
│   │   └── memory.py         # semantic_search, remember, recall, forget,
│   │                         # audit_history, suggest_doc_update, propose_doc_patch,
│   │                         # lint_run, lint_findings, lint_resolve
│   ├── systemd/              # service + timer units (templates)
│   ├── tests/                # pytest (pytest-postgresql)
│   └── deploy-mcp.sh         # idempotent production deploy script
│
└── aleph/                    # the 3D viewer
    ├── backend/              # FastAPI on 8765, reuses mcp.memory
    │   ├── main.py
    │   ├── db.py             # graph_snapshot + audit helpers
    │   ├── projection.py     # UMAP + HDBSCAN + top-k neighbors
    │   ├── mcp_bridge.py
    │   ├── auth.py
    │   ├── schema_additions.sql
    │   ├── triggers.sql      # LISTEN/NOTIFY on memories writes
    │   ├── requirements.txt
    │   └── tests/
    ├── frontend/             # Vite + React + Three.js
    │   ├── index.html
    │   ├── login.html        # custom Basic-Auth login
    │   ├── vite.config.js
    │   └── src/              # Scene, App, UI, store, api, styles
    ├── systemd/
    └── deploy-aleph.sh
```

---

## Quick start (local, 10 minutes)

1. **Prereqs**
   - macOS / Linux with Python 3.11+, Node 20+, PostgreSQL 16+, pgvector.
   - A GitHub repo with your documentation in Markdown under `content/`.
   - A Gemini API key (free tier works for bootstrap at scale).

2. **Clone + configure**
   ```bash
   git clone git@github.com:YOURORG/aleph-docs.git
   cd aleph-docs
   cp .env.example .env
   # Edit .env: set DOCS_REPO_URL, DOCS_REPO_TOKEN, GOOGLE_API_KEY, PG_DSN,
   # MCP_API_KEY, ALEPH_API_KEY, HTPASSWD_USER, HTPASSWD_PASSWORD
   ```

3. **Local Postgres + pgvector**
   ```bash
   brew install postgresql@16 pgvector     # Linux: use PGDG apt + postgresql-16-pgvector
   createdb aleph_memory
   psql aleph_memory -c "CREATE EXTENSION IF NOT EXISTS vector"
   psql aleph_memory -c "CREATE EXTENSION IF NOT EXISTS pgcrypto"
   psql aleph_memory -f mcp/memory/schema.sql
   psql aleph_memory -f aleph/backend/schema_additions.sql
   psql aleph_memory -f aleph/backend/triggers.sql
   ```

4. **MCP server**
   ```bash
   cd mcp
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python -m memory.bootstrap   # first-time embedding
   .venv/bin/python server.py             # http://127.0.0.1:8001
   ```

5. **Aleph viewer**
   ```bash
   cd ../aleph/backend
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/uvicorn main:app --reload --port 8765 &
   cd ../frontend
   npm install
   npm run dev     # http://localhost:5173/aleph/login.html
   ```

Full production bring-up (systemd, Apache reverse proxy, TLS, etc.) is in
[`SETUP.md`](SETUP.md).

---

## Connecting Claude Desktop (or any MCP client)

Once the MCP is running behind HTTPS:

```jsonc
// ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "aleph-docs": {
      "type": "url",
      "url": "https://your-domain.example/mcp",
      "headers": { "Authorization": "Bearer <MCP_API_KEY>" }
    }
  }
}
```

See [`mcp/PROJECT_INSTRUCTIONS.md`](mcp/PROJECT_INSTRUCTIONS.md) for a
system-prompt template that teaches Claude when to use which tool.

---

## Design references

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — diagrams + data flow + decay formula + cost model.
- [`mcp/memory/*`](mcp/memory) — the memory layer is the load-bearing piece; read `schema.sql` + `store.py` to understand the data model.
- [`aleph/prototype/HANDOFF.md`](aleph/prototype/HANDOFF.md) — original design notes for the 3D viewer (kept for reference; not loaded at runtime).

---

## License

See [`LICENSE`](LICENSE). Template code is MIT unless otherwise noted.

---

## Not included on purpose

- **Your documentation content.** Point `DOCS_REPO_URL` at your own git repo; the indexer will clone, watch and embed it.
- **Your secrets.** `.env.example` lists every variable; the real `.env` is gitignored.
- **Product-specific tools.** The MCP's `find_*` helpers are generic examples; add your own under `mcp/tools/` for domain-specific shortcuts.
- **A WordPress / CMS integration.** The original project this was extracted from had one; it's intentionally removed from the template. You can add a `tools/site.py` of your own if you want cross-source lookups.
