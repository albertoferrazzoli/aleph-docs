# Aleph Docs

**A reusable template for a documentation-aware LLM knowledge system that learns from use.**

![Aleph viewer — 3D semantic memory graph over a real docs corpus](assets/aleph-viewer.png)

*The Aleph 3D viewer on a live instance: blue nodes are `doc_chunk` memories
embedded from a Markdown repo, yellow are auto-recorded `interaction`
memories from search tools, pink are `insight` memories saved manually via
`remember()`. Edges are top-k cosine neighbors (solid ≥ 0.60, dashed
< 0.60). The right panel shows the selected chunk with its stability,
decay, access count and top-k neighbors; the bottom log streams inserts,
reinforcements and deletes live via Postgres LISTEN/NOTIFY.*

## What this is

Aleph Docs turns any Markdown documentation repository into a **living
knowledge system** that an LLM (Claude, ChatGPT, local models via MCP)
can both *read* and *write*. It gives you, out of the box:

- **One MCP server** that indexes your docs repo and exposes it to any
  LLM via the [Model Context Protocol](https://modelcontextprotocol.io/).
  Every call to a search tool implicitly accumulates context for future
  calls; every answer can be grounded and cited back to a line of Markdown.
- **One 3D web viewer** (*Aleph*) that shows the evolving knowledge graph
  in real time — so humans can inspect, curate, and navigate the memory
  with the same model the LLM is using.
- **A closed feedback loop** between the LLM interactions and the
  canonical Markdown repo: insights captured during support sessions can
  be promoted to pull requests on the docs repo with a single tool call.

In one sentence: **git-tracked docs become queryable, writeable, and
self-maintaining**, with costs measured in cents per year instead of
dollars per month.

## What problem it solves

Most teams who want "an LLM that knows our product" end up building
some variant of plain **RAG** (retrieve-chunks, stuff-into-prompt) or a
**Karpathy-style LLM-Wiki** (have the LLM generate and maintain
Markdown pages manually). Both patterns have known failure modes:

- Plain RAG is **stateless**. Every query rediscovers knowledge from
  scratch. Insights surfaced in one conversation don't enrich the next.
  The system never learns what's important vs. noise.
- LLM-Wiki is **expensive and slow**. Every write involves the LLM
  rereading pages, reconciling them, rewriting chunks. At scale, the
  token bill grows linearly with knowledge base activity.

Aleph Docs takes the best of both and removes the costly parts:

- It keeps Karpathy's *human-readable canonical source of truth* — the
  Markdown git repo. Git history is your audit log for "normative" facts.
- It adds an *operational fast layer* — a pgvector index with an
  Ebbinghaus-style forgetting curve — where interactions reinforce
  themselves, duplicates collapse automatically, and useless noise fades.
- It only calls the LLM where an LLM is strictly necessary (contradiction
  detection). Everything else — search, dedup, orphan detection,
  staleness — is plain SQL.

The result is a system that **gets smarter the more you use it**, costs
a few cents a year to maintain, and never loses the audit trail.

## Who it's for

- Customer support teams who want their knowledge base to accumulate
  customer-specific gotchas and workarounds without drift.
- Engineering docs owners who want "the docs" to include both the
  pristine prose in git *and* the field-tested knowledge from support.
- LLM-agent builders who need a fast, cost-predictable retrieval layer
  with proper write semantics (not just a vector-DB wrapper).
- Anyone who has tried "let the LLM maintain the wiki" and hit the
  token bill wall.

---

## Aleph Docs vs alternatives

| Dimension | **Plain RAG** (vector DB + prompt stuffing) | **LLM-Wiki** (Karpathy + Obsidian) | **Aleph Docs** (this) |
|---|---|---|---|
| **State across sessions** | None — stateless | Markdown files persist, curated by LLM | Vector memory + git Markdown, both tracked |
| **Retrieval latency** | Sub-200ms (vector only) | Multi-second (LLM rereads pages) | Sub-200ms (pgvector HNSW) |
| **Write cost per entry** | N/A (read-only) | High — LLM rewrites pages | ~$0.0005 (embedding only) |
| **Dedup** | None; same content re-indexed repeatedly | Up to the LLM to notice | Automatic: sim > 0.9 → reinforce instead of insert |
| **Freshness / decay** | None | None (files never decay) | Built-in: Ebbinghaus forgetting curve per row |
| **Contradictions** | Invisible — both hits rank equally | LLM lint finds them by rereading pages | Cheap SQL to find candidates, LLM judges only the top 20 |
| **Audit trail** | At best, vector-DB row history | `git blame` on `.md` files | Both: `memory_audit` table + git log of canonical repo |
| **Serendipity** (find unexpected connections) | Weak — similarity lost in prompt | Limited to explicit wikilinks | UMAP 3D projection surfaces latent clusters |
| **Visualization** | None (vectors aren't human-readable) | Obsidian 2D graph (explicit links only) | Real-time 3D viewer with decay, live writes, audit history |
| **Loop back to canonical docs** | None | Manual rewriting | `propose_doc_patch(open_pr=true)` opens a PR automatically |
| **Offline / local** | Easy (any local vector DB) | Easy (any local LLM + files) | Requires Postgres + Gemini (swappable for local embeddings) |
| **Predictable cost** | Low and flat | Grows with knowledge base size | Capped: SQL-free for most work, LLM budget hard-limited |
| **Typical yearly cost** | Embedding only | $10–$100s depending on LLM usage | $0.12 bootstrap + ~$0.06/year lint |

### Why not just plain RAG?

- **Memory evolution** — RAG doesn't remember that a user corrected it yesterday. Aleph Docs auto-records every search as an `interaction` memory, dedups by semantic similarity, reinforces what's actually useful, and decays what isn't touched.
- **Writable** — `remember(content, context)` stores a new insight in one call, addressable by UUID, retrievable by future semantic searches. Plain RAG has no write path; you re-run an indexing job and hope it picks things up.
- **Citation quality** — every answer can cite a concrete `source_path` (for docs) or a memory UUID (for insights), with an audit trail for each. RAG typically retrieves an opaque chunk with no provenance.

### Why not just LLM-Wiki + Obsidian?

- **Scale** — LLM-Wiki ingestion costs grow linearly with every new source. Aleph Docs ingests docs via deterministic chunking + embedding (no LLM in the loop), so bootstrap of thousands of pages is a few cents.
- **Write latency** — Saving a Markdown page via an LLM takes seconds. `remember()` returns in <1s regardless of how busy the LLM is.
- **No LLM-generated drift** — LLM-Wiki pages drift as the LLM rewrites them to reconcile new sources. Aleph Docs keeps canonical docs 100% human-edited (git-tracked); the LLM only proposes PRs — you merge them with human review.
- **Machine-queryable** — vector search is O(log N) with HNSW; "the LLM greps the wiki" is O(N token reads). At 10k+ memories, the difference is order-of-magnitude.

### What Aleph Docs keeps from each

- From **RAG**: sub-200ms vector retrieval, HNSW index, Gemini embeddings, cheap writes.
- From **LLM-Wiki**: Markdown files in git as the canonical source, PR review workflow, audit via `git log`, human-readable knowledge layer that survives vector DB resets.
- Added on top: forgetting curve, dedup, auto-reinforcement, visual graph, lint, explicit write/forget semantics, single-tool PR workflow back to canonical docs.

### When it's probably **not** the right tool

- You have **fewer than ~50 documents** total and prefer a plain wiki. Aleph's pgvector infrastructure is overkill.
- You need **fully offline / air-gapped**. The default stack uses Gemini for embeddings; swap to a local model (Ollama + BGE-M3) works but is not the default path.
- You don't have **any canonical docs** to index. Aleph assumes there's a git repo of Markdown to ground answers; if you only have scattered notes, adopt a minimal docs layout first.

---

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

## Where do your docs live?

Aleph Docs supports two modes — switch between them just by setting (or
leaving empty) the `DOCS_REPO_URL` env var:

| Mode | When to use | Source of docs |
|---|---|---|
| **Local** (default) | Solo devs, small teams, "just works" out of the box | The `./docs/` folder of this repo (commit your markdown alongside the app) |
| **Remote git repo** | Larger teams, docs have their own review cycle | Any GitHub repo — the indexer clones + pulls with a PAT |

In both modes the hourly `indexer.py --update` job picks up changes
incrementally; in remote mode it uses `git diff`, in local mode it uses
file mtimes. No code changes, just env flipping.

Drop a couple of `.md` files in `./docs/` and you can run the MCP without
a remote repo at all. See [`docs/README.md`](docs/README.md).

---

## Quick start (local, 10 minutes)

1. **Prereqs**
   - macOS / Linux with Python 3.11+, Node 20+, PostgreSQL 16+, pgvector.
   - A Gemini API key (free tier works for bootstrap at scale).
   - **Optionally**, a GitHub repo with your docs — or just use `./docs/`.

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
