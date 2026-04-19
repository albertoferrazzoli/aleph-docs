# Aleph

**Aleph** is a 3D web viewer for the long-term semantic memory of the
`aleph-docs-mcp` MCP server. It reads the `aleph_memory` Postgres database
(pgvector) and renders every memory as a node in an interactive space,
animated with the live stream of inserts / updates / deletes emitted by the
MCP.

Think of it as the **cerebrum viewer** of the knowledge system: docs chunks,
past interactions and manually saved insights are all projected into the
same 3D space where semantic proximity becomes physical proximity, and where
the Ebbinghaus-style forgetting curve is visible as fading colours and
shrinking halos.

- **Frontend**: Vite + React + plain Three.js (instanced mesh, >2k nodes at 60fps).
- **Backend**: FastAPI on `127.0.0.1:8765`, reuses the MCP `memory` package directly (single source of truth).
- **Projection**: hourly UMAP 3D + HDBSCAN cluster labels, cached into `graph_snapshot`.
- **Live updates**: Postgres `LISTEN/NOTIFY` on the `memories` table → FastAPI SSE → frontend patches in place.
- **Public URL**: `https://example.com/aleph/` behind Apache Basic Auth + a styled login page.

---

## Table of contents
- [Quick start](#quick-start)
- [How to use it — practical workflows](#how-to-use-it--practical-workflows)
- [Interface tour](#interface-tour)
- [Layouts](#layouts)
- [Filters and display modes](#filters-and-display-modes)
- [Query bar and search](#query-bar-and-search)
- [Selection and node panel](#selection-and-node-panel)
- [Audit history](#audit-history)
- [Remember and forget](#remember-and-forget)
- [Live updates](#live-updates)
- [Timeline / time shift](#timeline--time-shift)
- [Keyboard and camera](#keyboard-and-camera)
- [Authentication](#authentication)
- [Architecture](#architecture)
- [HTTP API](#http-api)
- [Environment variables](#environment-variables)
- [Local development](#local-development)
- [Production deploy](#production-deploy)
- [Troubleshooting](#troubleshooting)

---

## Quick start

Open https://example.com/aleph/ → the custom login page appears. Log in with
the `HTPASSWD_USER` / `HTPASSWD_PASSWORD` from `aleph/.env`. Optionally open
the **advanced** section and paste the `ALEPH_API_KEY` (required only for
`remember()` and `forget()`).

On successful login you land in the main app: a dark cosmic 3D scene with
the current memory snapshot rendered as colored nodes and translucent edges.

---

## How to use it — practical workflows

Aleph is not a "view for the sake of viewing" — it is an **operational tool
on top of the MCP memory**. This section describes concrete workflows you
can actually do from day one, ranked roughly by frequency.

### 1. Answer a support ticket grounded in past knowledge

**Goal**: a customer asks a question; you want the best answer backed by
both the official docs and any prior customer-support insight.

1. Type the question in the top-bar search box — e.g. *"how do I revoke a floating license"*.
2. Press Enter. The 15 most relevant memories pulse; the camera zooms onto the top-1.
3. **Click the top-1 node**. The right panel shows:
   - The `content` (Markdown, usually a full doc-section).
   - `source_path` — open the canonical .md on GitHub for authoritative quoting.
   - `top-k neighbors` — sibling chunks you may want to read.
4. If there's a **pink insight** among the neighbors, click it: that's a
   previously-captured customer-support note. It may contain the exact
   workaround you need.
5. Compose the reply. Cite the `source_path` (doc truth) + optionally the
   insight ("per a known gotcha we saw in ticket #...").

Tip: filter `FILTER KIND → only doc_chunk + insight` (uncheck interaction)
to remove search-history noise from the cloud.

### 2. Capture a reusable insight

**Goal**: you just solved a non-obvious problem and want future-you (or a
teammate, or Claude) to find this again without re-debugging.

1. Click **`+ remember()`** (bottom-right, pink).
2. Fill the form:
   - **content** (required): 1–3 concise sentences. Be literal: include
     error messages, flag names, file paths. This text is what will be
     embedded and searched — the clearer, the better the future match.
   - **context** (optional): ticket URL, customer name, date. Stored in
     metadata; not searched but visible in the right panel.
3. Click **commit insight**.
4. Within ~1 s a pink dot appears in the scene near the closest-semantic
   cluster. The event log at the bottom shows `+remember` with the new UUID.

Good insight examples:
- *"Customer running .NET 8 on Alpine Linux reports BBE-0041 on obfuscate. Resolved by installing `icu-data-full` and setting LANG=C.UTF-8 in the container."*
- *"Floating licenses behave incorrectly if two servers share the same MachineID. Fix: rebuild fingerprint with `aleph-lic fingerprint --force` on one of them."*

Bad insight examples (won't search well later):
- *"fixed that bug"* (no terms to match)
- *"see ticket 12345"* (no self-contained knowledge)

### 3. Explore the graph visually before a support shift

**Goal**: 5-minute warm-up on what the memory currently contains and where
activity has been.

1. Start on `layout = umap`. `SIZE BY → access`: big nodes are the FAQs.
   Note which regions of space they cluster in.
2. Switch to `layout = cluster`, `COLOR BY → source`. You see the 22-ish
   topic galaxies as coloured islands. Orbit them with drag.
3. `SIZE BY → decay`. Nodes shrink in real time — the dim corners are
   parts of the docs that nobody's touched recently; if a customer asks
   about one of those, expect the knowledge to be cold (not necessarily
   wrong, just un-rehearsed).
4. Drag the **time shift** slider backward: see what the memory looked
   like last week / last month. Useful to understand *how* certain
   topics gained prominence.

### 4. Time-travel debugging ("why is this memory so prominent now?")

**Goal**: a specific node shows unexpectedly high stability or access count.

1. Click the node.
2. Scroll the right panel to **audit history**: you see `insert` → `reinforce` → `reinforce` → … with timestamps and actors.
3. You can reconstruct: *"it was created 3 weeks ago, then hit by `search_docs` 6 times in one day — probably a recurring support topic"*.
4. If the reinforcement came from noisy auto-recorded interactions rather
   than genuine consultations, toggle `FILTER KIND → uncheck interaction`
   to visually re-balance.

### 5. Prune noise

**Goal**: kill off-topic or duplicate memories.

Two ways, depending on severity:

**Individual removal** (`forget()`):
- Click the noisy node → right panel → `forget()` button. Confirmed deletion.
- An audit row with the content snapshot is still preserved; nothing is
  cryptographically unrecoverable.

**Bulk noise suppression** (let decay do it):
- Do nothing. Unused memories fade automatically:
  `decay = exp(−age / stability)`. After ~7 weeks without retrieval, an
  interaction with initial stability=3d is effectively invisible at the
  default `MIN DECAY SCORE` threshold.
- If you want to hide them in the viewer immediately, raise
  `MIN DECAY SCORE` to 0.3 or 0.5.

### 6. Follow up on lint findings

**Goal**: act on the weekly lint report that Aleph generates automatically.

Lint runs every 6 h on the VM. Cheap checks (orphan / redundant / stale)
are free and continuous; the LLM-judge contradiction check runs at most
once a week.

To see current findings, from Claude Desktop (or any MCP client):

```
lint_findings(kind=null, include_resolved=false, limit=50)
```

Typical flow per finding kind:

| Finding | Suggested action |
|---|---|
| `orphan` insight | The insight has no nearby doc_chunk. Either add a `source_path` via a new insight that cross-references it, or verify the knowledge is still relevant and expand the content (more terms → better embedding → better grounding). |
| `redundant` pair | Two insights say roughly the same thing. Open both via `semantic_search`, pick the richer one, `forget()` the other. You could also merge: create a combined insight and forget both originals. |
| `stale` doc_chunk | The source .md has been edited but the embedding didn't refresh (usually hourly updater hadn't run yet, or the indexer errored). Check `journalctl -u aleph-docs-mcp-update` on the VM; run `python indexer.py --update` manually to force. |
| `contradiction` pair | Two insights disagree (LLM-judged). Read both, decide which is right in the *current* state of the product, `forget()` the stale one. |

After acting, mark the finding resolved:
```
lint_resolve(finding_id=123, note="kept the newer one, deleted the stale")
```

### 7. Close the loop: turn an accumulated insight into canonical docs

**Goal**: an insight you saved 6 weeks ago has been rediscovered 5 times by
auto-interactions — it's clearly recurring, it deserves to live in the
canonical Nextra docs, not just in the vector memory.

From Claude Desktop:

```
propose_doc_patch(topic="floating license revocation", top_k=8, open_pr=true)
```

This:
1. Gathers the top insights / interactions on that topic.
2. Picks the best target `.md` in `<DOCS_REPO_NAME>` by vector similarity.
3. Appends a *"Note dal supporto (auto-suggerite)"* block after the matching H2.
4. Creates a branch `docs/mcp-<slug>-<YYYYMMDD-HHMM>`, commits, pushes.
5. Opens a GitHub PR at `<DOCS_REPO_SLUG>`.

You open the PR, review the diff, edit the wording if needed, merge.

Within an hour, the MCP's indexer re-absorbs the merged content as a new
`doc_chunk` — at that point the original insight that triggered the PR can
be forgotten (it's now canonical). The `lint` might also flag it as
redundant against the new doc_chunk — you get a gentle nudge to retire it.

### 8. Batch onboarding of a new knowledge corpus

**Goal**: you have an existing Slack channel / email archive / Jira project
full of tribal knowledge and you want to import it.

Two patterns, complementary:

**A. Programmatic `remember()` loop** (fastest for raw notes):
```python
from mcp.session import ...   # or curl the MCP directly
for ticket in tickets:
    remember(
        content = summarize(ticket, max_chars=500),
        context = f"{ticket.id} {ticket.url}",
        source_path = None,
    )
```
Cost: ~$0.0005 per insight (Gemini embedding). 10k tickets ≈ $5 one-off.
Dedup is automatic: if an insight is >0.9 similar to one that exists, it
reinforces instead of duplicating.

**B. Markdown → doc_chunk** (best for reference material):
Write the curated knowledge as `.md` files in the `<DOCS_REPO_NAME>` repo
under `content/internal/...`, push. The hourly indexer picks them up and
embeds. They'll show up as `doc_chunk` (blue) in the scene.

Use B for stable, reviewed knowledge. Use A for everything that flows
from live operations.

### 9. Daily sanity check (30 seconds)

```bash
# From your laptop
curl -u alberto:$pw https://example.com/aleph/api/health
```

Expect:
```json
{"status":"ok","memory_count":<N>,"snapshot_version":<V>,"graph_nodes":<N>}
```
If `memory_count` dropped unexpectedly, check `memory_audit` for a
high-volume `delete` op. If `snapshot_version` is stale (hasn't advanced
in > 2h), trigger a projection rebuild.

---

## Interface tour

```
 ┌──────────────────── Top bar ────────────────────────────────┐
 │ brand · [semantic_search box] · stats · pg_listen indicator │
 ├───────────┬──────────────────────────────┬──────────────────┤
 │   LEFT    │                              │   RIGHT PANEL    │
 │  RAIL     │        3D CANVAS             │ (on node click)  │
 │ layouts   │                              │  kind + UUID     │
 │ filters   │                              │  content         │
 │ color by  │                              │  stability/acc   │
 │ size by   │                              │  neighbors       │
 │ edge cut  │                              │  audit history   │
 │           │                              │  isolate/forget  │
 ├───────────┴──────────────────────────────┴──────────────────┤
 │  time shift slider │ event log (live inserts / reinforces)  │
 ├────────────────────┴─────────────────────────────────────────┤
 │ legend · tweaks · sign out                  [+ remember()]   │
 └──────────────────────────────────────────────────────────────┘
```

### Top bar
- **Brand** (left): Aleph logo + subtitle.
- **semantic_search input** (center): type a natural-language query, press Enter.
- **Stats** (right):
  - `n` total visible memories (after current filters)
  - `doc` / `int` / `ins` breakdown per kind
  - **pg_listen** badge: green pulse = SSE stream connected, grey = disconnected.

### Left rail
Collapsible configuration column. See [Layouts](#layouts) and
[Filters and display modes](#filters-and-display-modes).

### 3D canvas
Main viewport. See [Keyboard and camera](#keyboard-and-camera).

### Right panel
Shown only when a node is selected. See [Selection and node panel](#selection-and-node-panel).

### Bottom bar
- Left: **time shift** slider ([jump](#timeline--time-shift) back in time).
- Right: rolling event log of the last 5 live events (insert / delete / reinforce / search / remember / forget).

### Floating elements
- **+remember()** (bottom right): manual insight composer. See [Remember and forget](#remember-and-forget).
- **Legend** (bottom right): edge style guide + keyboard shortcut reference + `tweaks` toggle + `sign out`.
- **Hover tip** (follows cursor): quick preview of any node under the pointer — kind, id, content, stability, access count, decay.

---

## Layouts

Three alternative 3D arrangements of the same nodes. Switch via the `LAYOUT`
segmented control; the camera auto-fits on every switch.

| Layout | What it is | When to use |
|---|---|---|
| **umap** | 1536-D embeddings projected to 3-D on the server with UMAP (cosine metric, centred + scaled to ±80). Semantically similar memories are physically close. | Default. Surveys the *shape* of the memory: topics, outliers, gaps. |
| **force** | Spring-directed graph: every top-k edge is a spring, every node repels every other. Simulation runs 140 iterations in the browser. | Find hubs, isolated components, verify a memory is well-connected. |
| **cluster** | Discrete galaxies: HDBSCAN cluster labels become points on a golden-angle sphere; each memory orbits its cluster centre with small jitter. | Navigate by topic. Use together with `COLOR BY → source` to see cluster membership at a glance. |

UMAP coordinates come precomputed from the backend (hourly timer). `force`
and `cluster` are recomputed client-side on demand and memoised per
snapshot version.

---

## Filters and display modes

### FILTER KIND
Three checkboxes (all enabled by default):
- `doc chunk` — pieces of canonical Nextra docs indexed automatically.
- `interaction` — queries recorded by the MCP search tools (`search_docs`, `semantic_search`, …).
- `insight` — notes saved manually via `remember()`.

Unticking a kind hides all its nodes (and edges touching them).

### MIN DECAY SCORE (slider 0–1)
Hides memories whose current `score = similarity × decay` is below the
threshold. `decay = exp(−age_days / stability)` drops toward 0 for
unaccessed memories. Examples:
- `0.05` (default): show almost everything except very-old never-reinforced.
- `0.30`: "what's in active memory right now".
- `0.90`: "what was touched today".

The filter is purely visual — nothing is deleted. Reinforcing a memory
(via a successful search) brings it back above threshold automatically.

### COLOR BY
- `kind` — blue = doc chunk, yellow = interaction, pink = insight.
- `stability` — cold→warm gradient. Blue = fragile (new memories, low stability). Red = entrenched (reinforced many times).
- `source` — deterministic hue per HDBSCAN cluster. Combine with `cluster` layout for a colour-coded topic map.

### SIZE BY
- `access` — `log(access_count + 1)`. Big = frequently retrieved.
- `stab` — proportional to stability (days). Big = persistent.
- `decay` — proportional to current decay score. Big = fresh in memory *today*.

`SIZE BY decay` + time shift slider is the best way to *see memory decaying*
— drag time back and the cloud fades.

### EDGE WEIGHT CUTOFF (slider 0.35–0.95)
Minimum similarity for an edge to be drawn. Solid line: `sim ≥ 0.60`.
Dashed line: `sim < 0.60`. Raise the slider to reveal only the skeleton of
strongest connections.

---

## Query bar and search

Type a natural-language query in the top bar and press Enter. The request
goes to `POST /aleph/api/search` which calls the MCP `store.search()`
function. That function:

1. Embeds the query via Gemini.
2. Runs a pgvector top-k with cosine + Ebbinghaus decay.
3. **Atomically reinforces** every returned memory (access_count +1, stability × 1.7, last_access_at = now).
4. Returns the top hits.

In the viewer the top-15 results pulse rhythmically; the top-1 becomes the
camera's zoom target. A search also fires an auto-recorded
`interaction` node (visible shortly as a new yellow dot via SSE) — this is
how the MCP accumulates query history.

Search is read-only from the user's perspective (no X-Aleph-Key needed).

---

## Selection and node panel

Click any node → the right panel opens with:

- **kind badge**: colored label (doc chunk / interaction / insight).
- **UUID**: the exact `memories.id`, copyable.
- **content**: the full text of the memory (doc chunks are the original
  Markdown section with its title + heading prefix).
- **source metadata**: `source_path` (for doc chunks and insights linked to
  a doc), and `tool` (for interactions: which MCP tool fired the record).
- **stats grid** (4 cells):
  - `stability` in days + filling bar
  - `access_count` total retrievals since creation
  - `decay score` current value (0…1) + filling bar
  - `last_access` relative age ("2h ago", "3d ago", "4mo ago")
- **top-k neighbors**: up to 8 nearest memories by cosine similarity, with
  weight bars (solid ≥ 0.60, dashed < 0.60). Click a neighbor to jump to
  it (the selection moves, camera zooms).
- **audit history**: see next section.
- **actions**:
  - `isolate neighborhood` — toggle: dim everything except selected + its direct neighbors. Shortcut `I`.
  - `forget()` — delete from DB. Irreversible, requires write key.

---

## Audit history

Every write operation on the memory layer is logged into `memory_audit`.
The right panel shows the last 20 events for the selected node:

| Column | Meaning |
|---|---|
| **op** | `insert` (green), `update` (blue), `delete` (red), `reinforce` (yellow), `access` (grey). Reinforce events are captured only when `AUDIT_REINFORCE=true`. |
| **ts** | Relative time ("12h ago"). |
| **actor** | Origin: `mcp:remember`, `mcp:forget`, `indexer:bootstrap`, `aleph:ui`, etc. |

Useful to answer questions like *"when did I save this insight?"*,
*"why is its stability so high?"*, *"who deleted the old version?"*.

Data is read from `GET /aleph/api/node/{id}/audit`.

---

## Remember and forget

### `+ remember()` — floating bottom-right button

Opens a composer:

| Field | Required | Meaning |
|---|---|---|
| content | yes | The insight text. 1–3 sentences is ideal. Becomes a new `insight` node. |
| context | no | Ticket URL, customer name, anything that helps future-you understand why you saved it. Stored in `metadata.context`. |

The backend:
1. Validates auth (Basic Auth + `X-Aleph-Key` for writes).
2. Calls MCP `store.insert_insight()` which embeds the content via Gemini.
3. Inserts into `memories` with `stability=14` (fresh), `access_count=0`.
4. Returns the new UUID.
5. The trigger `memory_change_trg` fires `pg_notify('memory_change', ...)`.
6. The SSE stream pushes `{op: insert, id, kind: insight, source_path}` to every connected client.
7. The viewer inserts the new pink node at the nearest cluster.

### `forget()` — in the right panel

Deletes the selected memory. Backend path: `POST /aleph/api/forget/{id}` →
MCP `store.forget()` → `DELETE FROM memories WHERE id = $1` → NOTIFY →
SSE `{op: delete, id}` → every viewer removes the node.

**There is no undo.** An audit row with the pre-delete snapshot is written
*before* the DELETE, so you can still recover the content via
`audit_history(subject_id=...)` on the MCP side.

Both actions require the `X-Aleph-Key` header (paste it once on the login
page advanced section; it's stored in `localStorage` as `aleph.write_key`).

---

## Live updates

The scene is never stale. A single persistent `EventSource` subscription
to `/aleph/api/graph/stream` receives deltas as they happen:

| SSE event | What the UI does |
|---|---|
| `memory_change` op=`insert` | Add a new node, position near nearest cluster, animate fade-in. |
| `memory_change` op=`update` | Refresh node content/stability/access_count in place. |
| `memory_change` op=`delete` | Fade out and remove. |
| `version_bump` | A new UMAP snapshot is ready; the frontend refetches `/graph` and animates a smooth transition. |
| `ping` | Keep-alive heartbeat every 15 s (invisible). |

Connection management is automatic: on disconnect the client reconnects
with exponential backoff (1 s → 30 s max).

---

## Timeline / time shift

Slider in the bottom bar, range `−120 days → now` in daily steps.

Effect: the frontend **recomputes decay as if the current moment were N
days in the past** (decay formula uses `age_days = (now_shifted − last_access_at) / 86400`).
Combine with `SIZE BY decay` to visually replay how fresh each memory
looked on that date — the cloud contracts backward as forgotten memories
shrink and recent ones hadn't been reinforced yet.

This is a *client-side* recomputation; the DB is never altered.

---

## Keyboard and camera

### Keyboard
| Key | Action |
|---|---|
| `Q` | Focus the semantic_search input. |
| `F` | Re-fit camera to the visible cloud (bounding sphere → optimal zoom). |
| `I` | Toggle isolate on the selected node. |
| `Esc` | Clear selection, query results, and isolate mode. |

### Mouse / trackpad
| Action | How |
|---|---|
| **Rotate** (orbit the camera) | drag with left button |
| **Pan** (move the target) | drag with right button. Trackpad Mac: two-finger click + drag, or Ctrl + click + drag. |
| **Zoom** | mouse wheel / pinch |
| **Focus a node** | left-click it — camera zooms, node becomes selected |
| **Close selection** | click on empty space, or `Esc` |

The camera auto-fits on initial load and on every layout change.
Pressing `F` fits again at any time, using only currently-visible
(non-hidden) nodes.

---

## Authentication

Two layers of auth, both required on write endpoints:

### Perimeter — Apache Basic Auth on `/aleph/api`
- `/etc/apache2/aleph.htpasswd` contains one bcrypt entry
  (`HTPASSWD_USER` / `HTPASSWD_PASSWORD` from `.env`).
- The static frontend (`/aleph/*`) is intentionally **unauthenticated**, so
  the custom login page can be served.
- All API calls under `/aleph/api/*` require `Authorization: Basic <b64>`.
- The login page sends an explicit `Authorization` header on its test fetch
  — the browser then caches the Basic Auth credentials for that realm, so
  subsequent `EventSource` calls (which can't set custom headers) inherit
  them.

### Application — `X-Aleph-Key` on writes
- `remember` and `forget` require the `X-Aleph-Key` header.
- The value is `ALEPH_API_KEY` in `/opt/aleph-docs/aleph/.env`.
- The login page's "advanced" section stores it in `localStorage` as
  `aleph.write_key`. Paste it once; it's sent automatically on every write.

### Sign out
Bottom-right corner → `sign out` button clears both storage keys and
redirects to `login.html?force=1`.

---

## Architecture

```
                         ┌───────────────────┐
                         │ example.com LB   │  (GCP LB terminates HTTPS)
                         └─────────┬─────────┘
                                   │ :80
                         ┌─────────▼─────────┐
                         │    Apache 2.4     │   /etc/apache2/sites-enabled/wordpress.conf
                         └─┬─────┬──────┬────┘
                           │     │      │
        /aleph/*  (static) │     │      │ /aleph/api/*   (Basic Auth)
                           │     │      │
 ┌─────────────────────────┘     │      └───────────────────────────┐
 │                               │                                   │
 ▼                               ▼                                   ▼
/opt/aleph-docs/aleph/frontend/dist      /var/www/html/wordpress              uvicorn :8765
(static SPA: index.html,      (WordPress)                          (FastAPI)
 login.html, assets)                                                │
                                                                    │ import
                                                                    ▼
                                                         /opt/mcp/memory/*
                                                         (reused from MCP: db, store,
                                                          embeddings, audit)
                                                                    │
                                                                    ▼
                                                         Postgres 16 + pgvector
                                                         aleph_memory DB
                                                         (memories, graph_snapshot,
                                                          memory_audit,
                                                          memory_lint_findings)
                                                                    ▲
                                                                    │ LISTEN memory_change
                                                                    │
                                                                    │ hourly timer
                                                                    │ python -m backend.projection
                                                                    │ (UMAP + HDBSCAN)
```

### Systemd units on the VM
- `aleph-backend.service` — FastAPI/uvicorn, always on, User=www-data.
- `aleph-projection.service` + `.timer` — oneshot every hour, writes `graph_snapshot`.

### Folder layout
```
aleph/
├── prototype/          original standalone HTML prototype (reference only)
├── frontend/
│   ├── index.html      main SPA
│   ├── login.html      public login page
│   ├── vite.config.js  multi-page build + dev proxy
│   └── src/
│       ├── main.jsx
│       ├── App.jsx              top-level state, auth guard, SSE wiring
│       ├── Scene.jsx            Three.js scene (instanced mesh, picking, cam)
│       ├── UI.jsx               TopBar, LeftRail, RightPanel, BottomBar, etc.
│       ├── api.js               fetch wrapper + EventSource + auth helpers
│       ├── store.js             zustand store + derived layouts
│       ├── clientLayouts.js     client-side force & cluster layouts
│       └── styles.css           cosmic IBM Plex theme
├── backend/
│   ├── main.py                  FastAPI app
│   ├── db.py                    wraps memory.db; adds graph_snapshot + audit helpers
│   ├── projection.py            UMAP + HDBSCAN + pgvector top-k job
│   ├── mcp_bridge.py            search / remember / forget proxies
│   ├── auth.py                  X-Aleph-Key dependency
│   ├── schema_additions.sql     graph_snapshot DDL
│   ├── triggers.sql             memory_change trigger + notify function
│   ├── requirements.txt
│   └── tests/
├── systemd/
│   ├── aleph-backend.service
│   ├── aleph-projection.service
│   └── aleph-projection.timer
├── deploy-aleph.sh              idempotent deploy (see below)
├── .env.example
├── .env                         (gitignored; holds real secrets)
└── README.md                    (this file)
```

---

## HTTP API

Base path in production: `https://example.com/aleph/api`.
All endpoints are behind Apache Basic Auth. Write endpoints additionally
require `X-Aleph-Key`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/health` | basic | Liveness + memory_count + current snapshot version. |
| `GET`  | `/graph?version=<n>` | basic | Latest `graph_snapshot` payload `{nodes, edges, version}`. If `version` matches current: `{version, unchanged: true}`. |
| `GET`  | `/graph/stream` | basic | SSE stream of `memory_change` and `version_bump` events + 15 s pings. |
| `POST` | `/search` | basic | `{query, kind?, limit, min_score}` → list of hits (and triggers interaction reinforcement in the MCP). |
| `GET`  | `/node/{id}` | basic | Full node detail + top-k neighbors (no embedding). |
| `GET`  | `/node/{id}/audit?limit=N` | basic | Audit trail for a specific node. |
| `POST` | `/remember` | basic + write | `{content, context?, source_path?, tags?}` → `{id}`. Calls MCP `store.insert_insight`. |
| `POST` | `/forget/{id}` | basic + write | Deletes the memory. Returns `{deleted: bool}`. |

Response shapes follow `prototype/HANDOFF.md`. Nodes expose:
```
id, kind, content, source_path, source_section, metadata,
embedding_3d [x,y,z], cluster (int|null),
created_at, last_access_at, access_count, stability
```
Edges: `{a, b, w}` with integer indices or UUID strings (the store normalises).

---

## Environment variables

Defined in `/opt/aleph-docs/aleph/.env` on the VM (600, owned by `www-data`) and
locally in `aleph/.env` for dev. See `.env.example`.

| Var | Default | Purpose |
|---|---|---|
| `ALEPH_HOST` | `127.0.0.1` | uvicorn bind host. |
| `ALEPH_PORT` | `8765` | uvicorn port. |
| `ALEPH_API_KEY` | (required) | Secret for the `X-Aleph-Key` header on writes. |
| `MEMORY_ENABLED` | `true` | Must be true; if false the backend returns structured errors. |
| `PG_DSN` | (required) | `postgresql://aleph:PASSWORD@localhost:5432/aleph_memory`. |
| `MCP_PATH` | `/opt/mcp` | Path to the MCP package so `from memory import store` works. |
| `GOOGLE_API_KEY` | (required for writes) | Needed by `memory.embeddings` when `remember()` is called. |
| `EMBED_MODEL` | `gemini-embedding-001` | Must match what the MCP uses. |
| `EMBED_DIM` | `1536` | Must match the `memories.embedding` column. |
| `LOG_LEVEL` | `INFO` | Uvicorn/aleph logger level. |

And, used only by `deploy-aleph.sh` (read from *local* `.env` on your Mac):
`PG_PASSWORD`, `HTPASSWD_USER`, `HTPASSWD_PASSWORD`.

---

## Local development

Prereqs: Python 3.11+, Node 20+, local Postgres with `aleph_memory`
(the MCP repo's `deploy-aleph-docs-mcp.sh` sets it up in one command if
you want to mirror prod locally).

```bash
cd aleph

# 1. Backend
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env     # fill in PG_DSN + GOOGLE_API_KEY + MCP_PATH
backend/.venv/bin/uvicorn backend.main:app --reload --port 8765

# 2. Frontend (second shell)
cd frontend
npm install
npm run dev              # Vite dev server on :5173, proxies /aleph/api → 8765

# 3. Open http://localhost:5173/aleph/login.html — the dev proxy makes
#    authentication flow identical to prod.
```

To seed some data locally: run the MCP `memory.bootstrap` once so the
`memories` table has doc chunks; then manually `python -m backend.projection`
to produce the first `graph_snapshot`.

Test the backend without hitting the network:
```bash
cd backend
PG_TEST_DSN=postgresql:///aleph_test pytest tests/ -v
```

---

## Production deploy

```bash
cd aleph
# Fill in the local .env with:
#   ALEPH_API_KEY, PG_PASSWORD, GOOGLE_API_KEY, HTPASSWD_USER, HTPASSWD_PASSWORD
./deploy-aleph.sh                     # full deploy
./deploy-aleph.sh --skip-frontend     # backend + projection only
./deploy-aleph.sh --skip-apache       # never touch wordpress.conf
./deploy-aleph.sh --skip-pg           # skip schema/trigger re-apply
```

The script is idempotent and safe to re-run. It:
- Preserves `/opt/aleph-docs/aleph/.env` and `/opt/aleph-docs/aleph/data/` across re-runs.
- Runs `npm ci && npm run build` on your Mac, ships `dist/`.
- Installs / upgrades deps via pip with `--quiet`.
- Applies the Postgres schema + trigger with `CREATE ... IF NOT EXISTS`.
- Backs up `/etc/apache2/sites-enabled/wordpress.conf` before each edit.
- Uses markers (`# --- ALEPH BEGIN ---` / `END`) to replace its own block.
- Runs `apache2ctl configtest` and **restores the backup if it fails**.
- Uses `systemctl reload apache2` (not restart) so connections survive.
- Enables `aleph-backend.service` + `aleph-projection.timer`.

After deploy: `curl -u alberto:$pw https://example.com/aleph/api/health`.

---

## Troubleshooting

### The login popup still appears
You're hitting a cached Apache config. Do
`sudo systemctl reload apache2` on the VM or just re-run
`./deploy-aleph.sh` (which refreshes the marker block).

### 502 on `/aleph/api/...`
```bash
gcloud compute ssh <VM_NAME> --zone=europe-west1-d \
  --project=<GCP_PROJECT> --command='sudo systemctl status aleph-backend && sudo journalctl -u aleph-backend -n 80'
```
Common causes: missing `PG_DSN`, missing `MCP_PATH`, `memory` package
missing from the venv (transitive: `tenacity`, `google-genai` must be
installed).

### 401 on login with correct credentials
`/etc/apache2/aleph.htpasswd` missing or malformed — re-run
`./deploy-aleph.sh` (it rewrites the file).

### SSE disconnects after ~30 s
The Apache block defines `<Location /aleph/api/graph/stream>` with
`timeout=86400`, `flushpackets=on` and `X-Accel-Buffering: no`. If the
block was hand-edited the SSE will stall. Re-deploy — the markers-based
replace will restore it.

### Audit history is empty on old nodes
Doc chunks inserted before the `memory_audit` hook was added
(pre-`feat(aleph-docs-mcp): audit trail` commit) have no audit rows. New
`remember()` and `forget()` populate audit going forward. To backfill,
reset the `memories` table and re-run `python -m memory.bootstrap`.

### "big white blob" instead of a spread cloud
Your `graph_snapshot` is stale or the UMAP scale is off. Trigger a
rebuild: `sudo systemctl start aleph-projection.service` on the VM and
reload the browser.

### Apache config broken after deploy
`/etc/apache2/sites-enabled/wordpress.conf.bak.YYYYMMDD-HHMMSS` holds the
last working version. Restore with:
```bash
sudo cp wordpress.conf.bak.<ts> wordpress.conf
sudo systemctl reload apache2
```

### Health checks
```bash
# On the VM, internal
sudo -u www-data curl -sS http://127.0.0.1:8765/health

# Public (replace <pw>)
curl -u alberto:<pw> https://example.com/aleph/api/health

# Logs
journalctl -u aleph-backend -f
journalctl -u aleph-projection -f
```

---

## Related docs

- `mcp/PRD_SEMANTIC_MEMORY.md` — the PRD of the memory system that Aleph sits on.
- `mcp/README.md` — MCP server: the write side of the graph,
  the auto-indexing pipeline, the `remember` / `forget` /
  `propose_doc_patch` / `lint_run` MCP tools.
- `aleph/prototype/HANDOFF.md` — original design notes for the 3D viewer.
