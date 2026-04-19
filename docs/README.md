# Your documentation goes here

Drop your `.md` / `.mdx` files anywhere inside this folder (subdirectories
welcome). The MCP indexer will pick them up on every run of
`indexer.py --update` and embed them into the pgvector memory.

## How it works

By default, `mcp/indexer.py` reads from this directory when the environment
variable `DOCS_REPO_URL` is **empty** (local mode). The relative path of
each `.md` / `.mdx` file becomes its `source_path` in the database, and the
first top-level directory becomes its `section`.

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
