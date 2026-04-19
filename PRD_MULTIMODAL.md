# PRD — Native Multimodal Memory (Gemini Embedding 2)

**Owner**: TBD
**Status**: Draft for review
**Target**: Aleph Docs main branch
**Expected effort**: ~3 engineer-days
**Dependencies**: Google `gemini-embedding-2-preview` access, ffmpeg on the
deploy host

---

## 1. Context

Aleph Docs today embeds only Markdown text via Google
`gemini-embedding-001` (1536-d). With the release of
[Gemini Embedding 2](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-embedding-2/)
(model id `gemini-embedding-2-preview`) — a natively multimodal embedder
that outputs 768/1536/3072-d Matryoshka vectors — we can extend the system
to index images, video, audio and PDFs **in the same vector space**, with
no schema migration and no second provider.

A single `semantic_search("crash on windows")` query would then retrieve
text chunks, UI screenshots, bug screencasts and customer voice notes from
one index.

The rest of the stack (pgvector HNSW, forgetting curve, auto-reinforcement,
audit trail, lint, UMAP viewer) is already modality-agnostic. This PRD
scopes the minimal changes needed to turn a documentation memory into a
unified multimodal memory, **without regressing the existing text path**.

---

## 2. Goals / non-goals

### 2.1 Goals

- **G1**: Embed images, short videos, short audio clips, and PDF pages with
  `gemini-embedding-2-preview`, storing them in the same `memories` table
  as doc chunks.
- **G2**: Route discovery: the indexer walks a `content/` tree containing
  mixed modalities and dispatches each file to the right chunker.
- **G3**: Unified retrieval: `semantic_search(query, kind?)` returns hits
  across modalities, ranked by the same `similarity × Ebbinghaus-decay`
  formula.
- **G4**: The Aleph viewer renders each `kind` appropriately: text, image,
  video (with frame seek), audio (with waveform + player), PDF page link.
- **G5**: Zero regression on the existing text path — the migration is
  opt-in via env vars and a one-shot re-bootstrap.
- **G6**: Cost and latency envelopes documented and bounded.

### 2.2 Non-goals

- LLM caption generation for every image/video/audio chunk. Captions are
  optional and, when present, supplied by the user or by a separate
  caption tool — not added by this PRD.
- Streaming video indexing (live feeds). This PRD targets static files.
- Fine-grained image understanding (bounding boxes, segmentation, OCR on
  embedded text). Out of scope; users who want it add a post-processor.
- Cross-index deduplication across modalities (e.g. "this audio
  transcript and that text chunk are redundant"). The lint's
  contradiction/redundant checks operate per-kind for now.

---

## 3. Functional requirements

| ID | Requirement |
|---|---|
| F1 | The embedder module accepts heterogeneous input lists (`str`, `bytes`+`mime_type`, or `pathlib.Path`) and produces 1536-d vectors via `gemini-embedding-2-preview` with MRL output. |
| F2 | New `memories.kind` values: `image`, `video_scene`, `audio_clip`, `pdf_page` (in addition to `doc_chunk`, `interaction`, `insight`). |
| F3 | Images: 1 file = 1 memory row; `media_ref` stores the file path (or URL); `preview_b64` stores a ≤ 20 KB thumbnail for the viewer. |
| F4 | Videos: ffmpeg extracts keyframes on scene change (or every N seconds if SCENEDETECT fails); each keyframe becomes a `video_scene` memory with `metadata.t_start_s` and a thumbnail; embedding is computed on a ≤ 120s clip centred on the keyframe. |
| F5 | Audio: ffmpeg splits the file into ≤ 80s segments; each becomes an `audio_clip` memory with `metadata.t_start_s` / `t_end_s`. |
| F6 | PDFs: one memory per page or per batch of ≤ 6 pages; `media_ref` = `{file}#page={n}`. |
| F7 | `semantic_search(query, kind?)` supports the new kinds, returning the same shape as today plus `media_ref` / `media_type` / `preview_b64`. |
| F8 | New MCP tool `remember_media(path, context?)` accepts an absolute local path and embeds the file with the appropriate chunker. |
| F9 | The viewer's right panel uses a modality-specific renderer (text block / image / video player / audio player + waveform / PDF link) based on `media_type`. |
| F10 | The indexer in local mode walks `.md`, `.mdx`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.mp4`, `.mov`, `.mp3`, `.wav`, `.pdf` under the docs root. Extension → chunker routing is table-driven and easy to extend. |
| F11 | Audit rows continue to include a content snapshot; for media, the snapshot is the `content` field (caption or transcript excerpt), not the raw bytes. |
| F12 | Lint checks `orphan` / `redundant` / `stale` keep working per kind. The `contradiction` LLM judge is updated to accept multimodal pairs (images included) by passing the `media_ref` blobs to the judge model. |

---

## 4. Non-functional requirements

- **N1** — **Backward compatibility**: unchanged `EMBED_MODEL` env keeps the current behaviour. Upgrading is opt-in.
- **N2** — **Schema stability**: new columns are `ADD COLUMN IF NOT EXISTS` on `memories`. No re-embedding of existing rows is required for mixed-mode operation (though recommended for best cross-modal retrieval).
- **N3** — **Cost envelope**: per-request embedding at 1536-d is similar to v1 for text; video/audio requests are billed per second of content. Document an estimated cost per 1000 files per modality (see §12).
- **N4** — **Latency**: `remember_media` returns within 10s for a 50MB video or 120s audio clip. Images and PDF pages: < 3s.
- **N5** — **Graceful degradation**: if ffmpeg is missing, video/audio ingest reports a structured error without crashing the MCP.
- **N6** — **Privacy**: media files are NOT stored in the DB. Only thumbnails (≤ 20 KB, base64) and a reference path. Deletion via `forget(id)` does not touch the source file.
- **N7** — **Idempotent indexer**: identical file (same hash + mtime) skipped on re-run, same as today.

---

## 5. Architecture

### 5.1 New / modified modules

```
mcp/
├── memory/
│   ├── embeddings.py       (M)  ← accepts Part-like contents, model switchable
│   ├── chunker.py          (M)  ← unchanged for markdown
│   ├── chunker_media.py    (N)  ← image / video / audio / pdf chunkers
│   ├── media.py            (N)  ← ffmpeg wrappers + thumbnail helpers
│   ├── schema.sql          (M)  ← ADD COLUMN media_ref / media_type / preview_b64
│   └── bootstrap.py        (M)  ← walks multi-ext, dispatches
├── indexer.py              (M)  ← extension table driven
└── tools/memory.py         (M)  ← new tool remember_media(path, context?)
aleph/
├── backend/
│   └── main.py             (M)  ← serve /media/{id} for full-res fetch
└── frontend/src/
    ├── UI.jsx              (M)  ← modality-aware right-panel renderer
    └── Scene.jsx           (~)  ← optional: COLOR BY media_type
```

### 5.2 Schema additions (idempotent)

```sql
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS media_ref    TEXT,
    ADD COLUMN IF NOT EXISTS media_type   TEXT,
    ADD COLUMN IF NOT EXISTS preview_b64  TEXT;

CREATE INDEX IF NOT EXISTS memories_media_type_idx ON memories(media_type);
```

The kind ENUM is already permissive; the check constraint (if any) is
replaced by an in-app allowlist to avoid ALTER-on-ENUM friction.

### 5.3 Embedder signature

```python
# memory/embeddings.py
from google.genai import types

EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-2-preview")
EMBED_DIM   = int(os.getenv("EMBED_DIM", "1536"))

async def embed_batch(contents: list) -> list[list[float]]:
    """`contents`: list of (str | genai.types.Part | pathlib.Path).

    Paths are read + auto-typed by suffix. Parts are passed through.
    Strings are embedded as text. The SDK concatenates multimodal
    interleaved input up to 8192 tokens per request (v2 limit).
    """
    parts = [_to_part(c) for c in contents]
    # batches of 1 per modality are safe; for text you can batch up to SDK max
    res = await client.aio.models.embed_content(
        model=EMBED_MODEL,
        contents=parts,
        config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
    )
    return [e.values for e in res.embeddings]
```

### 5.4 Media chunker outlines

**Image** (`chunker_media.chunk_image`)
- Verify MIME is `image/png` or `image/jpeg` (convert via Pillow if needed).
- Generate a 256×256 thumbnail, base64-encode → `preview_b64` (cap 20 KB).
- `content` = caller-supplied caption OR the filename stem.
- `metadata` = `{"sha256": ..., "w": ..., "h": ..., "bytes": ...}`.

**Video** (`chunker_media.chunk_video`)
- Probe with ffprobe: duration, resolution, codec.
- Segment: either scene-detect via `ffmpeg -vf "select=gt(scene,0.4)"` or fixed windows of 60s if scenedetect fails.
- For each segment ≤ 120s, extract a keyframe thumbnail (PNG).
- Embed the segment as a `types.Part.from_bytes(..., mime_type='video/mp4')` — trimmed via ffmpeg to the segment bounds.
- `metadata` = `{"sha256_src": ..., "t_start_s": t0, "t_end_s": t1, "duration_s": ..., "codec": ...}`.

**Audio** (`chunker_media.chunk_audio`)
- Probe with ffprobe.
- Segment into ≤ 80s windows with 2s overlap.
- For each segment, embed via `types.Part.from_bytes(..., mime_type='audio/wav')`.
- Optional: run Whisper in a separate pipeline to populate `content` with a transcript for FTS fallback (this is an enrichment, not required).

**PDF** (`chunker_media.chunk_pdf`)
- Use `pypdfium2` to count pages and render each page to a 256×256 thumbnail.
- Batch ≤ 6 pages per embedding request (v2 limit).
- One memory row per page with `media_ref = "file.pdf#page=3"`.

### 5.5 Indexer dispatch table

```python
DISPATCH = {
    ".md":   chunk_markdown,   # existing
    ".mdx":  chunk_markdown,
    ".png":  chunk_image,
    ".jpg":  chunk_image, ".jpeg": chunk_image, ".webp": chunk_image,
    ".mp4":  chunk_video, ".mov":  chunk_video,
    ".mp3":  chunk_audio, ".wav":  chunk_audio,
    ".pdf":  chunk_pdf,
}
```

The `--update` timer uses mtime in local mode (unchanged) and git-diff in
remote mode; for remote mode, media changes are detected via git too.

---

## 6. Tool surface

### 6.1 New MCP tools

| Tool | Signature | Purpose |
|---|---|---|
| `remember_media(path, context?, tags?)` | `(path: str, context: str = "", tags: list[str] = [])` → `{id, kind, ...}` | Embed a local media file as a new memory. `kind` is inferred from extension. Requires `X-Aleph-Key` for writes (same auth as `remember`). |
| `media_info(memory_id)` | `(id: str)` → `{media_ref, media_type, duration_s?, page?, ...}` | Retrieve media-specific metadata without downloading the blob. |

### 6.2 Existing tools — behaviour changes

- `semantic_search(query, kind?)` — `kind` can now be one of the new values; default (no filter) returns all modalities. Response items gain `media_ref`, `media_type`, `preview_b64` fields (null for text-only kinds).
- `suggest_doc_update` / `propose_doc_patch` — continue to operate only on `doc_chunk` + `insight`. Media is ignored (they can't be inlined into a `.md` PR).

### 6.3 Aleph viewer HTTP API additions

- `GET /aleph/api/media/{memory_id}` — streams the raw media bytes from `media_ref` behind Basic Auth. Content-Type from `media_type`. Required so the frontend can render full-resolution images/audio/video without exposing `media_ref` paths.

---

## 7. Migration strategy

### 7.1 Coexistence mode (default)

`EMBED_MODEL=gemini-embedding-2-preview` is set in `.env`. New writes use
v2. Existing `gemini-embedding-001` rows keep working for same-modality
search (text↔text), but cross-modal retrieval will underperform until
those rows are re-embedded.

### 7.2 Full migration (recommended)

One-shot re-embed of all text `doc_chunk` rows:

```bash
# On the VM
cd /opt/aleph-docs/mcp
sudo -u www-data CONFIRM_REEMBED=yes \
    .venv/bin/python -m memory.bootstrap --reembed-all
```

Cost for ~1000 text chunks: ~\$0.15 at v2 preview pricing (TBD confirmed;
see §12). The pgvector HNSW index is rebuilt automatically on first INSERT
after a TRUNCATE, or incrementally as rows are UPSERTed.

### 7.3 Rollback

`EMBED_MODEL=gemini-embedding-001` reverts new writes to v1. Rows
embedded with v2 are NOT compatible in the same vector space, so a full
rollback requires either:
- Keeping the pre-v2 pg_dump and restoring, or
- Re-bootstrap with v1 (another ~\$0.15).

---

## 8. Rollout phases

| Phase | Scope | Duration |
|---|---|---|
| 0 | Feature branch + refactor `embeddings.py` for heterogeneous inputs. Unit tests with mocked SDK. | 0.5 day |
| 1 | Schema additions + image chunker + `remember_media` tool + viewer image renderer. Dogfood on 10 screenshots. | 1 day |
| 2 | Video chunker + audio chunker + ffmpeg dependency + deploy script update. Dogfood on 5 videos + 5 audio clips. | 1 day |
| 3 | PDF chunker + batch tuning. End-to-end with a mixed docs corpus in `./docs/`. | 0.5 day |
| 4 | Full re-bootstrap of text with v2 + `semantic_search` regression tests. | 0.5 day |

Total wall time: ~3.5 engineer-days.

---

## 9. Verification / acceptance

End-to-end success is defined as:

1. `./mcp/deploy-mcp.sh --skip-frontend` on a clean VM with v2 env succeeds,
   `/health` shows `memory_count > 0` with the new columns.
2. `remember_media("/tmp/screenshot.png")` returns a UUID within 3s.
   The Aleph viewer shows the thumbnail within 1s (via SSE).
3. `semantic_search("login page broken")` returns BOTH the markdown
   troubleshooting chunk AND the screenshot, ranked together.
4. Clicking the screenshot node in Aleph opens it full-resolution via
   `/aleph/api/media/{id}`.
5. `lint_run(mode="cheap")` runs to completion over a mixed corpus,
   reports per-kind counts without errors.
6. `forget(media_id)` removes the DB row; the viewer fades the node within
   1s; the audit row shows `op=delete` with the content snapshot.

Unit tests must cover:
- `embeddings.embed_batch` with mixed text + Part inputs (mock SDK).
- `chunker_image` thumbnail size + base64 validity.
- `chunker_video` segmentation boundaries.
- `chunker_audio` window overlap correctness.
- `chunker_pdf` page batching at 6-page boundary.
- Indexer dispatch to the right chunker per extension.

---

## 10. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `gemini-embedding-2-preview` rate limits on free tier | High | Medium | Exponential retry via tenacity (already in `embeddings.py`). Document paid-tier cadence. |
| v2 pricing far higher than v1 | Medium | High | Cost cap + hard limit on daily tokens via `EMBED_DAILY_CAP` env (new). Fall back to v1 for text-only if cap exceeded. |
| Breaking API change during "preview" phase | Medium | High | Pin `google-genai>=X.Y` in requirements.txt. Watchdog test in CI. |
| Cross-modal relevance mediocre | Low | Medium | Benchmark text-to-image retrieval on a labeled set post-deploy; if below 70% P@5 revisit chunking. |
| ffmpeg absent on deploy host | Medium | Low | Deploy script adds `apt install -y ffmpeg`. `chunker_media` returns structured error otherwise. |
| PDF > 6 pages per batch — silent truncation | Low | Medium | Split at library level; test with a 100-page PDF. |
| Storage growth from thumbnails | Low | Low | 20KB × 100k images = 2 GB — acceptable. Add `preview_b64` to `TOAST_COMPRESS_TEMPORARILY`. |

---

## 11. Out of scope

- Caption/transcript generation (Whisper / BLIP). Users can add them as a separate pipeline that fills `content` before embedding.
- Image OCR and vector-DB-based text inside screenshots.
- Spatial / bounding-box object retrieval ("find the button in the image").
- Live streaming ingest.
- Cross-modal lint contradictions beyond the "is A and B contradictory" prompt supplied to the vision-capable judge.
- Multi-tenant isolation by modality.

---

## 12. Cost model (pending pricing confirmation)

Assumes v2 pricing is within 2x of v1 (\$0.075/1M input tokens for 001).
Update this section when official pricing publishes.

| Workload | Unit | Est. cost |
|---|---|---|
| Text chunk (avg 500 tokens) | per 1000 chunks | \$0.04–\$0.08 |
| Image | per image | \$0.0003–\$0.0008 |
| Video segment (60s) | per minute | \$0.001–\$0.003 |
| Audio segment (60s) | per minute | \$0.0005–\$0.0015 |
| PDF page | per page | \$0.0003–\$0.0006 |

Example corpus cost:
- 1000 text chunks + 500 screenshots + 50 videos (10 min avg) + 20 audio
  (30 min avg) + 30 PDFs (20 pages avg) ≈ **\$2–\$6 one-off bootstrap**.
- Incremental writes (`remember` / `remember_media`): negligible in practice.

---

## 13. Open questions (before starting Phase 0)

1. **Model ID stability**: is `gemini-embedding-2-preview` the final ID or
   will it change to `gemini-embedding-2-ga` at GA? Pin loosely.
2. **Rate limits**: documented per-minute request cap? Batch size cap?
   Need to test empirically.
3. **Vision-judge for contradictions**: does `gemini-2.5-flash` already
   accept images in the current SDK? If yes the lint change is trivial.
4. **Embedding stability across versions**: will embeddings change on
   minor model updates during preview? Plan for `--reembed-all` as a
   safety valve.
5. **Media storage location**: in-tree under `docs/media/` or a bucket
   (`gs://`, `s3://`)? This PRD assumes local paths; bucket support is a
   follow-up requiring a `media_ref` URL resolver.

---

## 14. Reference links

- Google announcement: https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-embedding-2/
- SDK docs: https://ai.google.dev/gemini-api/docs/embeddings
- Existing PRD (text-only memory): see `mcp/PROJECT_INSTRUCTIONS.md` §1–3 and `README.md` §What you get.
