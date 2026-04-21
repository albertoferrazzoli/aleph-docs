# Your documentation goes here

Drop **anything** inside this folder — markdown (`.md`/`.mdx`), PDFs,
videos (`.mp4`/`.mov`), audio (`.mp3`/`.wav`), images (`.png`/`.jpg`/
`.jpeg`/`.webp`). Organise subfolders however you like; the indexer
picks up every supported file regardless of where it sits in the tree.

For every file, the right chunker runs automatically: markdown by
heading, PDF by page (with embedded images extracted separately),
video by scene (ffmpeg keyframe detection), audio by ≤80 s overlapping
windows. Each resulting chunk is embedded and stored in pgvector.

## How it works

**Two change-detection modes**, selected by whether `DOCS_REPO_URL` is
set in `.env`:

- **Local mode** (`DOCS_REPO_URL` empty — default): this folder is
  bind-mounted into the containers. The MCP server walks it at boot,
  diffs against the database by `source_path + content hash`, and
  runs `add / update / delete` as needed. A filesystem watcher keeps
  the index in sync while the container runs — drop a new file and it
  appears as a coral-pink node in the viewer within a few seconds.
- **Git mode** (`DOCS_REPO_URL` set): this folder is ignored and the
  indexer clones the remote repo instead. Changes are detected via
  `git diff --name-status` between commits — no filesystem watcher;
  git itself is the source of truth for add/update/delete. This mode
  is more accurate (no races with partial writes, no false positives
  from editor save-rename cycles) and is recommended for shared
  corpora.

Initial ingest runs in the background after server startup — the MCP
server is reachable immediately, and `/health` reports progress
(`{"ingest": {"state": "running", "processed": N, "total": M,
"current": "..."}}`) until it finishes. Set
`INGEST_MEDIA_ON_BOOT=false` in `.env` to skip the boot scan and only
run reconciles on demand via the `reindex_docs` MCP tool.

The relative path of each `.md` / `.mdx` file becomes its `source_path`
in the database, and the first top-level directory becomes its
`section`.

Example layout:

```
docs/
├── guides/
│   ├── getting-started.md
│   ├── first-steps.md
│   └── ...
├── reference/
│   ├── cli.md
│   ├── api.md
│   └── ...
└── troubleshooting/
    └── common-errors.md
```

Sections above would be `guides`, `reference`, `troubleshooting`. The
`search_docs(section='guides', query=...)` tool filters by section name.

## Markdown conventions

Two nice-to-haves that improve search quality:

1. **Frontmatter with `title`** — otherwise the first `# H1` is used,
   otherwise the filename.
   ```markdown
   ---
   title: Getting started
   ---

   # Getting started

   Your content here.
   ```
2. **Explicit H2 / H3 headings** — the chunker splits on them to produce
   semantically coherent units. A 10k-character page with no H2 becomes
   one big chunk, which embeds worse than five focused chunks.

## Switching to a remote git repo instead

If your docs live in their own GitHub repository (recommended for
larger teams, so the docs have their own review / versioning cycle),
set in `.env`:

```
DOCS_REPO_URL=https://github.com/YOURORG/your-docs.git
DOCS_REPO_BRANCH=main
DOCS_REPO_TOKEN=<PAT with repo:read on that repo>
```

When `DOCS_REPO_URL` is set, this local `docs/` directory is ignored — the
indexer clones the remote repo into `mcp/repo/` instead, and
`incremental_update` uses `git diff` to touch only changed files.

## `content/` subdirectory?

If you prefer a nested layout (compatible with Nextra / Docusaurus / many
static-site generators), put your markdown under `docs/content/` and set
`CONTENT_SUBDIR=content` in `.env` (default). To place markdown directly
under `docs/`, set `CONTENT_SUBDIR=` (empty).

---

**Delete this README when you add your first real document.** It exists
only to document the convention; you don't want it indexed.
