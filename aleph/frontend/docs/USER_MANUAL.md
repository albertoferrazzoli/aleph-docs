# Aleph — User Manual

A living 3D map of everything your documentation knows.

---

## What is Aleph?

Aleph is a viewer for a **semantic memory** — a database that has read all
your documentation (markdown files, WordPress pages, PDFs, images, video
and audio transcripts) and reorganised it as a graph. Every memory is a
node; every meaningful relationship between two memories is an edge.

You drive the graph by asking questions. Type *"how does the floating
license work?"* and Aleph lights up the corner of the corpus that
actually answers — not by keyword, but by **meaning**. The hits pulse,
the rest dims, the camera flies onto the strongest match. Click a node,
the right panel shows you the source page, eight semantically nearest
neighbours, and the audit trail of every time that memory has been
touched.

You also drive it by **using** it. Memories you keep coming back to grow
brighter and more durable; memories nobody touches fade. The system
copies the spaced-repetition logic of human memory: things that prove
themselves consolidate, things that don't slowly disappear. The 3D scene
is a real-time visualisation of that process.

### Who it's for

- **Documentation owners** who want to see what their corpus actually
  covers, where the gaps are, and which pages are stale.
- **Power users** who want to ask hard cross-document questions and get
  the *exact* paragraph back, not a search-engine link list.
- **Teams using LLMs** with this codebase as their RAG backend, who want
  visibility into what the model can and cannot see.

### Two things to know before anything else

1. **Two kinds of memory.** Everything ingested from outside (a doc, an
   image, a video transcript) is **ground truth** and never fades.
   Everything that lives only inside Aleph (your saved insights, your
   own search history) **decays** unless reinforced. This split is the
   single most important idea in the system — it is explained in detail
   in §3.
2. **Editing your docs is the canonical way to teach Aleph.** When a
   doc file changes, the indexer notices, re-chunks, re-embeds the
   parts that actually changed, and the graph rearranges. You don't
   train a model. You write the truth down and Aleph follows. Details
   in §3.

---

## 1. Quick start

Your first five minutes:

1. **Open the viewer.** A login page appears (`/login.html`); enter
   your credentials. The session persists until you sign out from the
   bottom-right of the right panel.

2. **Look at the cloud.** Each dot is a memory. Colour = kind (sky =
   doc chunk, green = image, pink = insight, …). Drag with the left
   mouse button to rotate, the right button to pan, the wheel to zoom.
   Press <kbd>F</kbd> any time to refit the view.

3. **Ask something.** Click the search box at the top (or press
   <kbd>Q</kbd>), type a real question, hit <kbd>Enter</kbd>. The hits
   pulse; everything else dims; the camera lands on the strongest
   match. Press <kbd>Esc</kbd> to clear.

4. **Open a node.** Click a pulsing node. The right panel shows the
   source page, the actual content, decay/stability/access stats, and
   the eight closest neighbours. Click any neighbour row to fly to it.

5. **Save a thought.** Bottom-right, click `+ remember()`, type a
   sentence describing something you just figured out, press *commit
   insight*. A new pink node appears immediately and is broadcast to
   anyone else viewing the same workspace.

That's the loop. Search, navigate, remember. The graph rearranges
itself behind you.

---

## 2. Anatomy of the screen

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TopBar      Aleph · workspace · search · stream · settings              │
├──────────┬──────────────────────────────────────────┬────────────────────┤
│          │                                          │                    │
│  Left    │            3D scene canvas               │   Right panel      │
│  rail    │            (graph of memories)           │   (selected node)  │
│          │                                          │                    │
│          │                                          │                    │
├──────────┴────────────────────────┬─────────────────┴────────────────────┤
│  BottomBar  time shift slider     │   event log (REINFORCE / SEARCH …)   │
└───────────────────────────────────┴──────────────────────────────────────┘
                              + remember()    Tweaks (gear)
```

- **Top bar** — workspace selector, search box, live-stream indicator,
  settings.
- **Left rail** — controls for layout, kind filtering, score threshold,
  colour/size mapping, edge density.
- **3D scene** — the graph itself. Interactive.
- **Right panel** — full record of the currently selected node.
- **Bottom bar** — time-shift slider on the left, live event log on
  the right.
- **Floating** — `+ remember()` button (bottom-right), Tweaks gear
  (top-right).

Most settings persist across reloads on a per-browser basis. The data
itself lives on the server.

---

## 3. The mental model — what fades, what doesn't (essential reading)

Aleph splits its memory into **two regimes**. Misunderstanding the split
is the single most common cause of confusion, so this section is
deliberately blunt.

### 3.1 Ground truth — never decays

Anything that comes from **outside** Aleph belongs here:

| Kind                 | What it is                                       |
|----------------------|--------------------------------------------------|
| `doc_chunk`          | A paragraph-sized section of a docs page         |
| `image`              | A picture stored under your media root           |
| `pdf_page`           | A rendered page of a PDF                         |
| `video_scene`        | A keyframe / scene segment from a video          |
| `audio_clip`         | A windowed slice of an audio file                |
| `video_transcript`   | The text transcription of a video scene          |
| `audio_transcript`   | The text transcription of an audio clip          |
| `pdf_text`           | The extracted text of a PDF page                 |

These are **the corpus**. They exist because something exists outside
the system: a markdown file on disk, a WordPress post in MariaDB, a
PNG under `public/img/`, an MP4 in your assets folder.

Their decay score is permanently `1.0`. They do not fade. The viewer
will never hide a doc chunk because of a forgetting curve. The only
way to make them disappear is to delete the source file (or section)
— and the next indexer pass will mirror that on its own.

**Implication for the time-shift slider:** sliding back to `−120d`
will not vanish any doc or media. It can only fade insights and
interactions (see below).

### 3.2 Volatile cognitive layer — does decay

Only two kinds live under the forgetting curve:

| Kind          | What it is                                      | Default stability |
|---------------|-------------------------------------------------|-------------------|
| `insight`     | A note you (or an LLM tool) explicitly saved    | 14 days           |
| `interaction` | A search query, recall, click — system-generated| 3 days            |

Their decay score follows the SuperMemo-2 / Ebbinghaus formula:

```
decay = exp(− days_since_last_access / stability)
```

Every time you (or any tool) **accesses** one — a search hit, a click,
an LLM `recall()` — its `stability` is multiplied by 1.7 (capped at
365 days) and its `last_access` resets to *now*. This is
"reinforcement". Repeated reinforcement → the memory consolidates.
Silence → the memory fades.

When the decay score drops below the *Min decay score* slider in the
left rail, the node is hidden from the canvas. It still exists in the
database — it just no longer competes for your attention.

### 3.3 The promotion loop (insights → docs)

The two regimes are connected by an explicit promotion path. The
intended flow:

1. You save an `insight` via `+ remember()` (or an LLM tool does).
2. The insight gets accessed often enough that you (or an LLM tool)
   notice a pattern: this is something the **docs themselves** should
   say.
3. You write a paragraph into the relevant documentation file.
4. The indexer notices the file changed, re-chunks it, embeds the new
   paragraph, and inserts a fresh `doc_chunk` node. It is now ground
   truth.
5. The original `insight` can be left to fade on its own, or
   explicitly *forgotten* via the right-panel `forget()` button.

This loop is the whole point. Without it, insights would just pile up
forever and never feed back into the canonical corpus. Aleph is
deliberately built to favour **reorganisation over accumulation**.

### 3.4 How edits to the docs flow back

If you change a documentation file, you don't need to do anything in
Aleph — it reorganises its own database automatically. The pipeline:

1. **Detection.** A timer (hourly for WordPress, on-the-fly for the
   markdown reconciler) checks `post_modified` / filesystem mtime
   against a watermark. Only changed pages move to step 2.
2. **Re-chunking.** The page is re-split into sections.
3. **Hash compare.** Each new chunk's content hash is compared against
   the chunk already stored. Same hash → no-op (no embedding cost).
   Different hash → re-embed and `UPDATE` in place. The node's edges
   and audit history are preserved; only the embedding refreshes.
4. **Section deletion.** Anchors that disappeared from the new version
   of the page are `DELETE`-d, with an audit entry.

You can rely on this loop. Editing the source is the **canonical**
way to extend or correct what Aleph knows. The system has zero
tolerance for re-embedding unchanged content — there is no scenario
where you pay for tokens twice for the same paragraph.

### 3.5 What the time-shift slider really does

Sliding back recomputes decay against `now − Δd`, but **only for the
volatile layer**. Canonical nodes stay at 1.0 throughout (they didn't
decay then, they don't decay now). You are watching the live
cognitive layer rewind — which insights were salient on which day,
not which documents existed when.

---

## 4. Common workflows

### 4.1 "Find me everything we say about X"

1. Press <kbd>Q</kbd>, type the concept (a phrase, not a keyword
   list), <kbd>Enter</kbd>.
2. Read the event log: `SEARCH "X" → 12 hits · top 0.62`.
3. The camera flies to the top hit. Inspect the right panel.
4. Click a neighbour to follow the cluster. Click empty space or press
   <kbd>Esc</kbd> to clear and try a new phrasing.

Aleph indexes both your text and the **labels visible inside images**
(via the cross-modal embedding), so a query like "*Enterprise license*"
will surface diagrams that visually contain the word "Enterprise" even
if their on-disk filename is just `licensing-overview.png`.

### 4.2 "What does the corpus look like overall?"

1. In the left rail, set *Layout* to `cluster`. The graph rearranges
   into galaxies, one per HDBSCAN cluster.
2. Hover a galaxy to see the dominant kind and a few representative
   contents.
3. Switch *Color by* to `source` to see which folders of your repo
   own which neighbourhoods.

### 4.3 "Are my docs covering everything?"

1. Filter to *kinds = `insight`* only.
2. Enable *isolate neighborhood* on a popular insight.
3. If the insight has **no** `doc_chunk` neighbours above the cutoff,
   it is **orphaned** — your team has a recurrent thought that no
   documentation page validates. Strong candidate for promotion to a
   real doc.

(For the linter version of this check, see the MCP `lint_run` tool —
it surfaces orphans, stale chunks, and contradictions automatically.)

### 4.4 "Did the indexer catch my edit?"

1. After saving the file, wait up to a minute for the reconciler.
2. Search for an exact phrase from the change.
3. Click the matching node, look at the *audit history* in the right
   panel. You will see an `update` event with timestamp matching the
   reconciler run.
4. If the audit shows no recent `update`, the indexer hasn't picked
   it up yet — usually because the file isn't under the configured
   docs root (`/opt/docs/babel` in the babelfor.net deployment).

### 4.5 "What was the memory like a week ago?"

1. Drag the time-shift slider in the bottom bar leftwards to `−7d`.
2. The volatile layer fades to its decay state from a week ago. The
   canonical layer is unchanged.
3. Use this to spot which insights were vivid then but have decayed
   since, or to see whether a recent burst of activity has shifted
   the centre of gravity of your knowledge base.

The shift is **visualisation only** — it does not rewrite the
database.

### 4.6 "Save what I just figured out"

1. Click `+ remember()` (bottom-right).
2. **content** — the actual sentence or paragraph. This is what
   semantic search will match against; phrase it the way you'd want
   someone else to find it.
3. **context** *(optional)* — a short framing string ("project X",
   "edge case: empty payload"). Stored on the metadata, not embedded.
4. Click *commit insight*. The new pink node appears immediately. If
   anyone else is viewing the same workspace it appears on their
   canvas live too.

---

## 5. The Top bar

- **Workspace selector** — when multiple workspaces are configured,
  pick the corpus you want to look at. Each workspace has its own
  database, its own embedding model, and its own embedding
  dimensionality; hovering shows backend and dimensionality.
- **Search box** — semantic search across all modalities. Press
  <kbd>Q</kbd> to focus from anywhere, <kbd>Enter</kbd> to fire,
  <kbd>Esc</kbd> to clear.
- **Stream indicator** — the green `stream` dot is solid when the
  live SSE feed is connected. Hollow means the graph won't react to
  remote changes until reconnected.
- **Settings (⚙)** — write-key administration (see §13).

---

## 6. The Left rail

Five sections, all persistent across reloads.

### 6.1 Layout

Three live projections of the same graph:

| Mode      | What it shows                                                                          |
|-----------|----------------------------------------------------------------------------------------|
| `umap`    | Server-side UMAP. Best for finding semantic neighbourhoods.                            |
| `force`   | Client-side spring layout over the top-k edges. Best for inspecting a small selection. |
| `cluster` | HDBSCAN groups arranged as galaxies. Best for getting a feel of how many distinct themes the corpus has. |

### 6.2 Filter kind

Three groups, ten kinds:

- **text** — `doc_chunk`, `insight`, `interaction`
- **media** — `image`, `video_scene`, `audio_clip`, `pdf_page`
- **transcripts** — `video_transcript`, `audio_transcript`, `pdf_text`

Toggling a checkbox **hides** matching nodes (not just dims them). The
toolbar above each group offers *all* / *none* / *invert*; the chevron
collapses the group.

### 6.3 Min decay score

Memories whose `similarity × decay` is below this threshold are
hidden. `0.0` shows everything; `0.5` shows only the strongest
matches. Use it to declutter, especially when running broad searches.

### 6.4 Color by

- `kind` — hard category colour (default).
- `stability` — gradient from cool (fresh) to warm (consolidated).
- `source` — colour-codes by source path, so docs from the same area
  cluster visually.

### 6.5 Size by

- `access` — node size proportional to lifetime hits.
- `stab` — proportional to stability.
- `decay` — proportional to current decay score.

### 6.6 Edge weight cutoff

Slider over the edge similarity. Edges with weight `≥ 0.60` are drawn
solid; below that they are dashed. The cutoff itself controls **how
many** edges are rendered — pull right to declutter, left to expose
weaker associations.

---

## 7. The 3D scene

### 7.1 Navigation

| Gesture       | Effect                                  |
|---------------|-----------------------------------------|
| Left-drag     | Rotate the camera around the focal point. |
| Right-drag    | Pan.                                    |
| Wheel         | Zoom in / out.                          |

The camera auto-rotates if `auto-rotate` is on in Tweaks. Press
<kbd>F</kbd> to recenter and refit.

### 7.2 Hover

Pointing at a node shows a tooltip with kind, id, content excerpt,
stability, hit count, and decay score. The tooltip itself is
non-interactive; aim at the node's halo (broad hit area) rather than
its tiny core.

### 7.3 Click

Clicking opens the right panel and dims everything that is not a
top-k neighbour, so you can read the local cluster. Click empty space
or press <kbd>Esc</kbd> to clear.

### 7.4 Halo and pulse

Every node has a soft halo. Search hits and freshly reinforced
memories *pulse* (a brief expansion-and-fade) so you don't need to
open the panel to know what just happened.

---

## 8. The Right panel (node detail)

Selecting a node reveals its full record:

- **Header** — coloured kind tag, full id, source path and section.
- **Media renderer** — for image/video/audio/pdf nodes the asset is
  rendered inline (image preview, HTML5 player with timestamp seek
  for video/audio, page preview + open-pdf link for PDFs).
- **Stats grid**:
  - *stability* — days, with a bar relative to the 365-day cap.
  - *access_count* — lifetime hits.
  - *decay score* — current freshness; clamped to 1.0 for canonical
    kinds.
  - *last_access* — relative time.
- **Top-k neighbors** — up to eight semantically closest memories,
  ranked by similarity. Click any row to fly to that node.
- **Audit history** — chronological event log for this single id
  (insert, update, reinforce, access, delete) with timestamps and
  the actor that performed each operation.
- **Actions**:
  - *isolate neighborhood* / *exit isolate* — hide everything except
    the selected node and its top-k. Same as <kbd>I</kbd>.
  - *forget()* — permanently delete the memory. Confirmation is
    enforced server-side; the action is auditable.

---

## 9. The Bottom bar

### 9.1 Time shift

Slider from `−120d` to `now`. Drag leftwards to recompute decay
against a past instant. The current offset is shown in days
(`−45d`, `now`, …).

Remember: only the volatile layer fades. Canonical nodes stay at 1.0
no matter how far back you go. The shift is **visualisation-only**.

### 9.2 Event log

Live tail of the last few events:

| Tag         | Colour | Meaning                                                              |
|-------------|--------|----------------------------------------------------------------------|
| `REINFORCE` | green  | A memory was strengthened (search hit, recall, manual access).       |
| `SEARCH`    | sky    | A query ran. The message shows `"<query>" → N hits · top <score>`.   |
| `REMEMBER`  | pink   | A new insight was committed.                                         |
| `FORGET`    | red    | A memory was deleted.                                                |

---

## 10. The Remember box

Bottom-right. The `+ remember()` button expands into a small form
with two fields: **content** (what you want to commit) and **context**
*(optional)*. Press *commit insight* to write. The new node appears
immediately as a pink `insight`.

Tip: phrase the content as if someone else were searching for it
later. Semantic search matches on the content field; the context
field is metadata and is **not** embedded.

---

## 11. The Tweaks panel

Open the gear icon (⚙) at the top right. Per-browser preferences:

- **mood** — `dark cosmic` (default) or `technical minimal`. Changes
  background, halos, and decoration density.
- **starfield** — toggles the cosmic backdrop.
- **auto-rotate** — slow continuous rotation around the focus point.
- **live updates** — enables / disables the SSE-driven graph
  refresh. Turn off to freeze the canvas while you reason about what
  you see.
- **decay curve** — model used for client-side recomputation when
  the time-shift slider is moved:
  - `Ebbinghaus (exp)` — `exp(−Δt/stability)`, the default.
  - `linear` — straight-line falloff.
  - `step` — sharp drop after the stability horizon.

---

## 12. Keyboard shortcuts

These work whenever the focus is **not** in an input or textarea.

| Key                | Action                                                  |
|--------------------|---------------------------------------------------------|
| <kbd>Q</kbd>       | Focus the search box.                                   |
| <kbd>Enter</kbd>   | Run the typed query (when the box is focused).          |
| <kbd>F</kbd>       | Fit the view to the currently visible nodes.            |
| <kbd>I</kbd>       | Isolate / un-isolate the selected node's neighborhood.  |
| <kbd>Esc</kbd>     | Clear the selection, search results, and isolate mode.  |

The shortcut hint strip at the bottom-right of the canvas mirrors
these.

---

## 13. Settings dialog

The gear button next to the stream indicator opens a write-key dialog
that lets a logged-in administrator edit a small set of
installation-wide values (API keys, endpoint URLs, …) without
touching the `.env` file by hand. Values persist server-side and take
effect immediately. Most users never need it.

---

## 14. Reading the colours

| Kind               | Swatch                                       |
|--------------------|----------------------------------------------|
| `doc_chunk`        | sky blue                                     |
| `interaction`      | amber                                        |
| `insight`          | pink                                         |
| `image`            | green                                        |
| `video_scene`      | coral                                        |
| `audio_clip`       | violet                                       |
| `pdf_page`         | orange                                       |
| `video_transcript` | light coral *(paired with `video_scene`)*    |
| `audio_transcript` | light violet *(paired with `audio_clip`)*    |
| `pdf_text`         | light orange *(paired with `pdf_page`)*      |

Lighter shades indicate the **text-side companion** of a media item —
the extracted transcript or page text — so when both flavours of a
multimodal asset coexist they are visually paired.

---

## 15. Path stability invariant (for operators)

Because the same file can be reached by code running in different
"modes" (git-clone vs local checkout, full bootstrap vs hourly
incremental), the storage key — `source_path` — is **always anchored
to the repository root**, never to the scan scope. A file at
`<repo>/content/licensing/X.mdx` is always stored as
`content/licensing/X.mdx`, regardless of whether the indexer scanned
`<repo>/content/` or `<repo>/`. Without this invariant, mode flips
silently produce a parallel set of duplicate rows under an alternate
naming, and the corpus doubles after the next workspace-switch or
deploy.

You'll only see this section matter during ops; it has no effect on
the daily user experience.

---

## 16. Cost discipline (for operators)

Aleph is deliberately careful about LLM-API spend. Three layers
guarantee that no token is ever paid twice for the same content:

1. **Mtime watermark.** The wp-indexer fetches only posts whose
   `post_modified > last_seen`. Unchanged posts never reach the
   chunker.
2. **Hash compare.** For each chunk, the new `sha256(content)` is
   compared against the stored hash. Identical → skipped (no embed
   call).
3. **Workspace-meta gate.** On MCP restart, the markdown rebuild
   only runs if the active workspace name has changed since the
   last rebuild. Same workspace → zero re-embed on restart.

A healthy hourly indexer run on an unchanged corpus reports
`posts=0 chunks=0 inserted=0 updated=0` and emits **zero**
batchEmbedContents API calls. If you see ongoing embed traffic on
a quiet corpus, something is wrong.

---

## 17. Troubleshooting

| Symptom                                    | First check                                                                                                        |
|--------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| **Empty canvas / spinner forever**         | Is the *stream* dot solid? Hollow = SSE disconnect. Reload.                                                        |
| **Search returns no hits**                 | Lower *Min decay score*. Cross-modal hits live in a 0.2–0.4 band; an aggressive threshold filters them out.        |
| **Image previews are broken**              | The `MEDIA_ROOT` env var must point at the directory that contains the actual image files.                         |
| **`forget()` is greyed out**               | Your session has read-only privileges. Sign out and back in with a write-capable user.                             |
| **Tooltip flicker on tiny nodes**          | Aim at the halo, not the core. Hover hit-testing is halo-wide.                                                     |
| **Time-shift thumb visually moves but graph doesn't change** | The thumb is wired correctly; if no node reacts you may have hit a corpus where everything is canonical (decay = 1.0 always) — that's by design. |
| **My doc edit isn't reflected**            | Wait one indexer cycle (≤ 1 hour for WP, on-save for markdown). Then search a verbatim phrase and check the audit. |
| **Hourly billing shows steady cost on a quiet corpus** | Open §16. If `posts=0` runs are still doing batchEmbedContents calls, the upsert is rolling back due to a constraint and re-billing every cycle. |

---

## 18. Glossary

- **MCP** — *Model Context Protocol*. The server that owns the
  database of memories and exposes them to LLMs and to this viewer.
- **Workspace** — a self-contained corpus (its own DB, embedding
  model, dimensionality). One viewer can switch between many
  workspaces.
- **Kind** — coarse-grained type of a memory (`doc_chunk`, `image`,
  …).
- **Stability** — how long a memory should remain salient, in days.
- **Decay score** — current "freshness" of a memory in `[0, 1]`.
- **Reinforcement** — refreshing a memory by accessing it.
- **Top-k neighbours** — the `k` nodes closest to a given memory in
  the embedding space, above the edge-weight cutoff.
- **Audit history** — the per-id event log: every insert, update,
  reinforcement, access, and delete, with timestamps and actors.
- **Canonical** — externally ingested artefacts whose decay is
  permanently 1.0 (docs, images, transcripts).
- **Volatile** — `insight` and `interaction`, the two kinds that
  follow the forgetting curve.
- **Promotion** — turning a recurrent insight into a doc edit so it
  becomes canonical.
- **Indexer** — the process that reads source files and refreshes
  the canonical layer in the database.
- **Reconciler** — the same process viewed from a different angle:
  it diffs disk vs DB and applies the missing inserts/updates/deletes.
