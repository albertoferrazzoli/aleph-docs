# Aleph — User Manual

Aleph is a live 3D viewer for a semantic memory: every memory the underlying
MCP server knows about is rendered as a node, every cross-reference between
memories as an edge. The graph reacts in real time as the system reads,
writes, decays, and reinforces what it remembers.

This manual covers the on-screen interface only. For the data model, the
forgetting curve, and how memories are ingested, see the project README and
`PRD_MULTIMODAL.md`.

---

## 1. Logging in

Aleph is private. The first time you open the URL you are routed to a login
page (`/login.html`). Enter the credentials issued for your installation;
the session cookie is set automatically and persists until you sign out.

Sign-out is the **sign out** button at the bottom-right of the right panel.

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

Every node is a memory; size and colour map to its properties (configurable,
see §6 and §7). Edges connect semantically related memories; weight maps to
opacity and dash pattern.

---

## 3. The Top bar

- **Workspace selector** — when multiple workspaces are configured, the
  dropdown switches the corpus you are viewing. Each workspace has its own
  database, its own embedding model, and its own dimension; hovering an item
  shows backend and dimensionality.
- **Search box** — type any phrase and press <kbd>Enter</kbd>. Aleph runs a
  semantic search across **all** modalities (text, transcripts, images,
  PDFs, audio) and:
  - flags the matching nodes with a pulsing halo,
  - dims everything that is not a hit or a near-neighbour,
  - zooms the camera onto the top hit and reinforces it (the hit becomes a
    little more durable in memory; you'll see a `REINFORCE` event in the
    log).

  Press <kbd>Esc</kbd> to clear the result and restore the full view.
- **Stream indicator** — the green dot labelled `stream` is solid when the
  live SSE feed is connected. If it is hollow the graph will not react to
  remote changes until the connection is restored.
- **Settings (⚙)** — opens a small dialog used for write-key
  administration (see §11).

---

## 4. The Left rail

The rail is divided into five sections. All settings persist across reloads.

### Layout

Three projections of the same graph, switched live:

| Mode      | What it shows                                              |
|-----------|------------------------------------------------------------|
| `umap`    | Server-side UMAP. Best for finding semantic neighbourhoods.|
| `force`   | Client-side spring layout over the top-k edges. Best for inspecting a small selection. |
| `cluster` | HDBSCAN groups arranged as galaxies. Best for getting a feel of how many distinct themes the corpus has. |

### Filter kind

The corpus has 10 memory kinds, organised in three groups:

- **text** — `doc_chunk`, `insight`, `interaction`
- **media** — `image`, `video_scene`, `audio_clip`, `pdf_page`
- **transcripts** — `video_transcript`, `audio_transcript`, `pdf_text`

Each kind has its own colour swatch (visible in the legend at the bottom of
the canvas). Toggling a checkbox **hides** matching nodes from the canvas
entirely (not just dims them). The toolbar above each group offers
*all* / *none* / *invert* shortcuts; the small chevron collapses the group.

### Min decay score

Memories whose `similarity × decay` falls below this threshold are hidden.
Use it to declutter: at `0.0` everything is visible, at `0.5` only the
sharpest matches survive. The current value is shown next to the slider.

### Color by

- `kind` — hard category colour (default).
- `stability` — gradient from cool (fresh) to warm (consolidated).
- `source` — colour-codes by source path so docs from the same area cluster
  visually.

### Size by

- `access` — node size proportional to access count (popular = large).
- `stab` — proportional to stability (long-lived = large).
- `decay` — proportional to current decay score (vivid = large).

### Edge weight cutoff

A slider over the edge similarity. Edges with weight `≥ 0.60` are drawn
solid; below that they are dashed. The cutoff itself controls **how many**
edges are rendered — pull the slider right to declutter, left to expose
weaker associations.

---

## 5. The 3D scene

### Navigation

- **Left-drag** — rotate the camera around the focal point.
- **Right-drag** — pan.
- **Wheel** — zoom in / out.

The camera auto-rotates if `auto-rotate` is on in the Tweaks panel. Press
<kbd>F</kbd> at any time to recenter and fit the visible nodes.

### Hover

Pointing at a node shows a tooltip with kind, id, content excerpt,
stability, hit count, and decay. The tooltip itself is non-interactive —
move the cursor onto the node, not the tooltip.

### Click

Clicking a node opens the **right panel** (§6) and dims everything that is
not a top-k neighbour, helping you read the local neighbourhood. Click on
empty space to deselect. Pressing <kbd>Esc</kbd> also clears.

### Halo and pulse

Every node has a soft halo. Search hits and freshly reinforced memories
*pulse*: a brief expansion-and-fade animation that draws attention without
needing the panel.

---

## 6. The Right panel (node detail)

When a node is selected the right panel reveals its full record.

- **Header** — coloured kind tag, full id, source path and section.
- **Media renderer** — for image/video/audio/pdf nodes the actual asset is
  rendered inline (image preview, HTML5 player with timestamp seek for
  video/audio, page preview + open-pdf link for PDFs).
- **Stats grid** — four cells:
  - *stability* (days; bar relative to the 365-day cap).
  - *access_count* (lifetime hits).
  - *decay score* (current `exp(−Δt / stability)` for non-source kinds, or
    a fixed 1.0 for canonical sources like docs and media).
  - *last_access* (relative time).
- **Top-k neighbors** — up to eight semantically closest memories.
  Bar fills show similarity weight; solid ≥ 0.60, dashed below. Click any
  row to jump to that node.
- **Audit history** — chronological event log for this single id (insert,
  update, reinforce, access, delete) with timestamps and the actor that
  performed the operation.
- **Actions** —
  - *isolate neighborhood* / *exit isolate* — toggles an isolated view that
    hides everything except the selected node and its top-k neighbours.
    Same effect as pressing <kbd>I</kbd> with a node selected.
  - *forget()* — permanently deletes the memory. Confirmation is enforced
    server-side; the action is auditable.

---

## 7. The Bottom bar

### Time shift

A timeline from `−120d` to `now`. Drag the thumb leftwards to see the graph
*as if* time were rewound: decay scores recompute against the shifted
"now", so everything that has decayed since that point becomes visibly
fresher again. Useful for answering "what did the memory look like a week
ago?". The current offset is shown in days (`−45d`, `now`, …).

The shift only changes the visualisation — it does **not** rewrite the
database.

### Event log

The right half is a live tail of the last few events:

| Tag         | Colour   | Meaning                                            |
|-------------|----------|----------------------------------------------------|
| `REINFORCE` | green    | A memory was strengthened (search hit, recall hit, manual access). |
| `SEARCH`    | sky      | A query ran. The message shows `"<query>" → N hits · top <score>`. |
| `REMEMBER`  | pink     | A new insight was committed.                       |
| `FORGET`    | red      | A memory was deleted.                              |

---

## 8. The Remember box

Bottom-right corner: a `+ remember()` button that expands into a small
form.

- **content** — the text you want to commit. This is the node's `content`
  field and what semantic search will match against.
- **context** *(optional)* — short framing string (audience, project,
  caveats). Stored on the metadata.

Press *commit insight* to write. The new node appears immediately in the
graph (kind = `insight`, pink) and is broadcast to anyone else viewing the
same workspace via the live stream.

---

## 9. The Tweaks panel

Open the gear icon (⚙) at the top right; controls there are personal
preferences (per-browser, persisted locally):

- **mood** — `dark cosmic` (default) or `technical minimal`. Changes
  background, halos, and decoration density.
- **starfield** — toggles the cosmic backdrop.
- **auto-rotate** — slow continuous rotation around the focus point.
- **live updates** — enables / disables the SSE-driven graph refresh. Turn
  off to freeze the canvas while you reason about what you see.
- **decay curve** — model used for client-side recomputation when the
  Time-Shift slider is moved:
  - `Ebbinghaus (exp)` — `exp(−Δt/stability)`, the default.
  - `linear` — straight-line falloff.
  - `step` — sharp drop after the stability horizon.

---

## 10. Keyboard shortcuts

These work whenever the focus is **not** in an input or textarea.

| Key            | Action                                                  |
|----------------|---------------------------------------------------------|
| <kbd>Q</kbd>   | Focus the search box.                                   |
| <kbd>Enter</kbd> | Run the typed query (when the box is focused).        |
| <kbd>F</kbd>   | Fit the view to the currently visible nodes.            |
| <kbd>I</kbd>   | Isolate / un-isolate the selected node's neighborhood.  |
| <kbd>Esc</kbd> | Clear the selection, search results, and isolate mode.  |

The shortcut hint strip at the bottom-right of the canvas mirrors these.

---

## 11. Settings dialog

The gear button opens a write-key dialog that lets a logged-in
administrator edit a small set of installation-wide values
(API keys, endpoint URLs, …) without touching the `.env` file by hand.
Values are persisted server-side and take effect immediately. Most users
will never need it.

---

## 12. Reading the colours

| Kind               | Swatch    |
|--------------------|-----------|
| `doc_chunk`        | sky blue  |
| `interaction`      | amber     |
| `insight`          | pink      |
| `image`            | green     |
| `video_scene`      | coral     |
| `audio_clip`       | violet    |
| `pdf_page`         | orange    |
| `video_transcript` | light coral  *(paired with `video_scene`)* |
| `audio_transcript` | light violet *(paired with `audio_clip`)*  |
| `pdf_text`         | light orange *(paired with `pdf_page`)*    |

Lighter shades indicate the **text-side companion** of a media item — the
extracted transcript or page text — so when both flavours of a multimodal
asset coexist they are visually paired.

---

## 13. Decay model — what fades, what doesn't (READ THIS)

Aleph splits its memory into **two regimes**, and the distinction is the
load-bearing idea behind the whole system. Misunderstanding it leads to
deleting things you wanted to keep, or hoarding things that should have
been promoted.

### Ground truth — never decays (decay = 1.0 forever)

Anything that represents an **externally ingested artefact**:

- `doc_chunk` — markdown / WP / docs page sections
- `image`, `pdf_page`, `video_scene`, `audio_clip` — media
- `video_transcript`, `audio_transcript`, `pdf_text` — transcripts of media

These are the corpus. They exist because something exists *outside* Aleph
(a doc on disk, a WordPress post, a video file). They are refreshed by the
**indexer**, not by a forgetting curve. If you delete the source file,
the indexer removes its chunks. If you edit the source, the indexer
updates only the chunks whose content hash changed (no needless re-embed).

You will never see a doc chunk fade. That is by design.

### Volatile cognitive layer — decays (Ebbinghaus)

Only **two** kinds live under the forgetting curve:

- `insight` — explicit notes saved through the `+ remember()` button or
  an LLM tool call. They start with 14d of stability.
- `interaction` — search queries, recalls, clicks. They start with 3d.

Their decay score is `exp(−Δt / stability)`. Every access multiplies
stability by 1.7 (capped at 365d) and resets last_access. Accessed
repeatedly → consolidates. Ignored → fades.

### How insights become persistent

An insight that keeps getting accessed will eventually accumulate enough
signal that its content deserves to enter the canonical layer. The
intended flow is:

1. Insight gets reinforced enough that the operator (you, or an LLM tool)
   recognises a pattern.
2. The operator runs a **doc reorganisation pass**: the relevant insight
   text is rewritten into a documentation file (markdown, WP page, …).
3. The indexer ingests the new file → it becomes one or more `doc_chunk`
   nodes (canonical, no decay).
4. The original insight can now be left to fade or be explicitly forgotten.

This is how *interactive* knowledge gets *promoted* to *durable*
knowledge. Without this loop, insights would just accumulate forever and
never feed back into the corpus.

### How doc updates flow back into Aleph

If you modify a documentation file, Aleph reorganises its database
automatically. The pipeline is:

1. The wp-indexer (hourly via systemd timer) and the docs reconciler scan
   their sources and detect changed pages by `post_modified` /
   filesystem mtime watermark.
2. Each changed page is re-chunked and the new chunks' hashes are
   compared against what the database already has.
3. Hashes that match → no-op (no embed cost).
4. Hashes that differ → the row is `UPDATE`-d in place (existing edges
   and audit history preserved; embedding refreshed).
5. Anchors that no longer exist in the new version → `DELETE`-d, with an
   audit entry.

So editing your documentation is the **canonical** way to extend or
correct what Aleph knows. The system is built to favour reorganisation
over accumulation.

### Path stability invariant

Because the same file may be reached by code running in different
"modes" (git-clone vs local checkout, full bootstrap vs hourly
incremental), the storage key — `source_path` — is **always anchored to
the repository root**, not to the scan scope. A file at
`<repo>/content/licensing/X.mdx` is always stored as
`content/licensing/X.mdx`, regardless of whether the indexer scanned
`<repo>/content/` or `<repo>/`. Without this invariant, mode flips
silently produce a parallel set of duplicate rows under the alternate
naming, and the corpus doubles after the next workspace-switch or
deploy.

### What the time-shift slider really does

Sliding back in time recomputes decay against `now − Δd` for the volatile
layer only. Canonical nodes stay at 1.0 (they did not decay then either).
You are watching the live cognitive layer rewind — the pulse of which
insights were salient when, not which documents existed when.

---

## 14. Troubleshooting

- **Empty canvas / spinner forever** — check the *stream* indicator. Hollow
  means the SSE feed is disconnected; usually a transient network blip.
  Reload the page if it persists.
- **Search returns no hits** — try lowering *Min decay score*. Cross-modal
  hits (image / video) live in a lower-similarity band than text-text and
  can be hidden by an aggressive threshold.
- **"Forget()" greyed out** — the active session has read-only privileges.
  Sign out and back in with a write-capable user.
- **Tooltip flicker** — the tooltip is offset to the cursor; aiming exactly
  at the small core of a tiny node can be fiddly. Aim at the halo instead;
  hover hit-testing is halo-wide.
- **Time-Shift thumb doesn't seem to do anything** — make sure *live
  updates* is on (Tweaks panel) and that you have selected a curve in the
  Tweaks > *decay curve* dropdown. The shift only redraws when those are
  active.

---

## 15. Glossary

- **MCP** — *Model Context Protocol*. The server that owns the database of
  memories and exposes them to LLMs and to this viewer.
- **Workspace** — a self-contained corpus (its own DB, embedding model,
  dimensionality). One viewer can switch between many workspaces.
- **Kind** — coarse-grained type of a memory (doc_chunk, image, …).
- **Stability** — how long a memory should remain salient, in days.
- **Decay score** — current "freshness" of a memory in `[0, 1]`.
- **Reinforcement** — the act of refreshing a memory by accessing it.
- **Top-k neighbours** — the `k` nodes closest to a given memory in the
  embedding space, above the edge-weight cutoff.
- **Audit history** — the per-id event log: every insert, update,
  reinforcement, access, and delete, with timestamps and actors.
