"""Indexer for Aleph documentation repo.

Clones/pulls the <DOCS_REPO_NAME> repo, parses Markdown files, and populates
a SQLite FTS5 index at DOCS_DB_PATH.

Usage:
    python indexer.py --rebuild     # drop + rebuild everything
    python indexer.py --update      # pull + incremental update
    python indexer.py --stats       # print stats
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from memory import chunker as _memory_chunker  # noqa: F401
except ImportError:
    _memory_chunker = None


ENABLE_MEMORY_HOOK = (
    os.environ.get("MEMORY_ENABLED", "true").lower() == "true"
    and _memory_chunker is not None
)

_pending_embeds: list[tuple[str, list, int]] = []
# For md-image indexing: (rel_md_path, [(abs_image_path, alt_text)])
_pending_images: list[tuple[str, list[tuple[Path, str]]]] = []


# --- Markdown-referenced image walker -------------------------------------

_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+?)\)")
_HTML_IMG_RE = re.compile(r'<img\b([^>]*)>', re.IGNORECASE)
_HTML_ATTR_RE = re.compile(
    r'''(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)')''', re.IGNORECASE
)
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _resolve_image_path(
    url: str, md_dir: Path, repo_root: Path, public_root: Path
) -> Path | None:
    if url.startswith(("http://", "https://", "data:", "#", "mailto:")):
        return None
    # Strip query / fragment
    url = url.split("#", 1)[0].split("?", 1)[0]
    if not url:
        return None
    if url.startswith("/"):
        # Absolute URL — Nextra convention: try public/ first, then repo root.
        cand = (public_root / url.lstrip("/")).resolve()
        if cand.is_file():
            return cand
        cand = (repo_root / url.lstrip("/")).resolve()
        return cand if cand.is_file() else None
    # Relative to the MD file.
    cand = (md_dir / url).resolve()
    # Safety: must stay under repo_root.
    try:
        cand.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return cand if cand.is_file() else None


def _extract_image_refs(
    body: str, md_path: Path, repo_root: Path
) -> list[tuple[Path, str]]:
    """Return [(absolute_image_path, alt_text)] for every inline image
    reference in `body`. Resolves relative paths against the MD file
    and absolute paths (starting with /) against the repo's content
    root (Nextra convention: /images/x.png means public/x.png or
    <repo>/images/x.png). Skips external URLs, data URIs, and
    non-image extensions.
    """
    seen_paths: set[Path] = set()
    refs: list[tuple[Path, str]] = []
    md_dir = md_path.parent
    public_root = repo_root / "public"
    for m in _MD_IMG_RE.finditer(body):
        alt, url = m.group(1).strip(), m.group(2).strip()
        # Strip markdown title after URL:  ![alt](url "title")
        url = url.split(" ", 1)[0]
        p = _resolve_image_path(url, md_dir, repo_root, public_root)
        if p and p.suffix.lower() in _IMG_EXTS and p not in seen_paths:
            seen_paths.add(p)
            refs.append((p, alt))
    for m in _HTML_IMG_RE.finditer(body):
        attrs_blob = m.group(1)
        attrs: dict[str, str] = {}
        for am in _HTML_ATTR_RE.finditer(attrs_blob):
            k = am.group(1).lower()
            v = am.group(2) if am.group(2) is not None else (am.group(3) or "")
            attrs[k] = v
        url = (attrs.get("src") or "").strip()
        alt = (attrs.get("alt") or "").strip()
        if not url:
            continue
        p = _resolve_image_path(url, md_dir, repo_root, public_root)
        if p and p.suffix.lower() in _IMG_EXTS and p not in seen_paths:
            seen_paths.add(p)
            refs.append((p, alt))
    return refs


def set_memory_hook(enabled: bool) -> None:
    global ENABLE_MEMORY_HOOK
    ENABLE_MEMORY_HOOK = bool(enabled) and _memory_chunker is not None


REPO_URL = os.environ.get("DOCS_REPO_URL", "").strip()
REPO_BRANCH = os.environ.get("DOCS_REPO_BRANCH", "main")
REPO_PATH = Path(os.environ.get("DOCS_REPO_PATH", "repo")).resolve()
DB_PATH = Path(os.environ.get("DOCS_DB_PATH", "data/index.db")).resolve()

# Local docs directory. When DOCS_REPO_URL is empty, the indexer reads from
# this path instead of cloning a git repo. Default is `./docs` sibling to
# the MCP, so a fresh checkout of this template is immediately usable.
_default_local = Path(__file__).resolve().parent.parent / "docs"
LOCAL_DOCS_PATH = Path(os.environ.get("LOCAL_DOCS_PATH", str(_default_local))).resolve()

# Effective source mode: "git" when DOCS_REPO_URL is set, else "local".
DOCS_MODE = "git" if REPO_URL else "local"

# In local mode, the content root is LOCAL_DOCS_PATH itself (no clone step).
if DOCS_MODE == "local":
    REPO_PATH = LOCAL_DOCS_PATH

# Name of the subdirectory inside the docs root that contains the .md/.mdx
# files. Nextra / Docusaurus conventionally use "content"; a plain ./docs
# folder does not. We therefore default to "content" in git mode and "" in
# local mode. Override via the CONTENT_SUBDIR env var to force either.
_default_subdir = "content" if DOCS_MODE == "git" else ""
CONTENT_SUBDIR = os.environ.get("CONTENT_SUBDIR", _default_subdir)


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def _run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout.strip()


def _auth_url(url: str) -> str:
    """Inject DOCS_REPO_TOKEN into an https GitHub URL if set.

    Supports both classic PATs and fine-grained tokens via 'x-access-token'.
    """
    token = os.environ.get("DOCS_REPO_TOKEN", "").strip()
    if not token or not url.startswith("https://"):
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def ensure_repo() -> Path:
    """Ensure the docs source is available locally.

    - DOCS_MODE='git':   clone if missing, else git fetch + reset HEAD.
    - DOCS_MODE='local': just verify LOCAL_DOCS_PATH exists (create if not).
    """
    if DOCS_MODE == "local":
        REPO_PATH.mkdir(parents=True, exist_ok=True)
        return REPO_PATH

    clone_url = _auth_url(REPO_URL)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not REPO_PATH.exists():
        REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {REPO_URL} into {REPO_PATH}")
        r = subprocess.run(
            ["git", "clone", "--branch", REPO_BRANCH, "--depth", "50", clone_url, str(REPO_PATH)],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {r.stderr.strip()}")
    else:
        print(f"Pulling latest in {REPO_PATH}")
        r = subprocess.run(
            ["git", "-C", str(REPO_PATH), "fetch", "--depth", "50", clone_url, REPO_BRANCH],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git fetch failed: {r.stderr.strip()}")
        _run(["git", "-C", str(REPO_PATH), "reset", "--hard", "FETCH_HEAD"])
    return REPO_PATH


def current_commit() -> str:
    """Return the current HEAD sha, or an fs-mtime digest in local mode."""
    if DOCS_MODE == "local":
        # In local mode we don't have git metadata. Return a hash of the
        # max mtime across all files so incremental_update can still detect
        # changes. Gracefully degrades to "" if the dir is empty.
        import hashlib
        h = hashlib.sha256()
        for p in sorted(REPO_PATH.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".mdx"):
                try:
                    h.update(f"{p}:{p.stat().st_mtime_ns}".encode())
                except Exception:
                    pass
        return h.hexdigest()[:16]
    return _run(["git", "-C", str(REPO_PATH), "rev-parse", "HEAD"])


def git_log(since_commit: str | None = None, limit: int = 20) -> list[dict]:
    # In local mode there's no git history to show.
    if DOCS_MODE == "local":
        return []
    fmt = "%H%x09%ai%x09%an%x09%s"
    args = ["git", "-C", str(REPO_PATH), "log", f"-n{limit}", f"--pretty=format:{fmt}"]
    if since_commit:
        args.append(f"{since_commit}..HEAD")
    out = _run(args, check=False)
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return commits


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    rest = text[m.end():]
    fm = {}
    # Simple key: value parser (no full YAML dep). Works for Nextra's typical usage.
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
            elif v.startswith(('"', "'")) and v.endswith(('"', "'")):
                v = v[1:-1]
            fm[k] = v
    return fm, rest


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def extract_headings(body: str) -> list[dict]:
    headings = []
    in_code = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            headings.append({"level": level, "text": text, "anchor": slugify(text)})
    return headings


def extract_code_blocks(body: str) -> list[dict]:
    blocks = []
    for m in _CODE_BLOCK_RE.finditer(body):
        lang = (m.group(1) or "").lower()
        blocks.append({"language": lang, "content": m.group(2)})
    return blocks


def derive_title(fm: dict, headings: list[dict], path: Path) -> str:
    if fm.get("title"):
        return str(fm["title"])
    for h in headings:
        if h["level"] == 1:
            return h["text"]
    # Fallback: prettify filename
    name = path.stem
    if name == "index":
        name = path.parent.name
    return name.replace("-", " ").replace("_", " ").title()


def section_of(rel_path: Path) -> str:
    parts = rel_path.parts
    if not parts:
        return "root"
    # If the path is a single file under content/ (e.g. 'index.mdx'),
    # treat it as the 'root' section rather than using the filename.
    if len(parts) == 1:
        return "root"
    return parts[0]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    path TEXT PRIMARY KEY,
    section TEXT NOT NULL,
    title TEXT,
    frontmatter TEXT,
    content TEXT NOT NULL,
    headings TEXT,
    mtime INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_section ON pages(section);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    path UNINDEXED,
    title,
    content,
    section UNINDEXED,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS code_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    language TEXT,
    content TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_code_blocks_path ON code_blocks(path);
CREATE INDEX IF NOT EXISTS idx_code_blocks_lang ON code_blocks(language);

CREATE VIRTUAL TABLE IF NOT EXISTS code_blocks_fts USING fts5(
    content,
    language UNINDEXED,
    path UNINDEXED
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def iter_doc_files(repo_root: Path):
    # If CONTENT_SUBDIR is empty, the repo_root itself holds the markdown.
    content_dir = repo_root / CONTENT_SUBDIR if CONTENT_SUBDIR else repo_root
    if not content_dir.is_dir():
        raise RuntimeError(
            f"Docs content directory not found: {content_dir}. "
            f"Create it or set LOCAL_DOCS_PATH / CONTENT_SUBDIR accordingly."
        )
    for p in content_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".md", ".mdx"):
            yield p


def upsert_page(conn, rel_path: str, abs_path: Path):
    raw = abs_path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(raw)
    headings = extract_headings(body)
    code_blocks = extract_code_blocks(body)
    rel = Path(rel_path)
    section = section_of(rel)
    title = derive_title(fm, headings, abs_path)
    mtime = int(abs_path.stat().st_mtime)

    conn.execute("DELETE FROM pages WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM pages_fts WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM code_blocks WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM code_blocks_fts WHERE path = ?", (rel_path,))

    conn.execute(
        "INSERT INTO pages(path, section, title, frontmatter, content, headings, mtime) VALUES(?,?,?,?,?,?,?)",
        (rel_path, section, title, json.dumps(fm), body, json.dumps(headings), mtime),
    )
    conn.execute(
        "INSERT INTO pages_fts(path, title, content, section) VALUES(?,?,?,?)",
        (rel_path, title, body, section),
    )
    for cb in code_blocks:
        conn.execute(
            "INSERT INTO code_blocks(path, language, content) VALUES(?,?,?)",
            (rel_path, cb["language"], cb["content"]),
        )
        conn.execute(
            "INSERT INTO code_blocks_fts(content, language, path) VALUES(?,?,?)",
            (cb["content"], cb["language"], rel_path),
        )

    # Memory hook: collect chunks for later async batch embedding.
    if ENABLE_MEMORY_HOOK and _memory_chunker is not None:
        try:
            chunks = _memory_chunker.chunk(rel_path, body, fm, headings)
            if chunks:
                _pending_embeds.append((rel_path, chunks, mtime))
        except Exception as e:
            logging.getLogger("memory").warning(
                "[memory] chunking %s failed: %s", rel_path, e
            )
        # Markdown-referenced image collection — one image memory per
        # ![alt](path) or <img src=…> found in the body. Resolved paths
        # must live under REPO_PATH; external URLs are skipped.
        try:
            refs = _extract_image_refs(body, abs_path, REPO_PATH)
            if refs:
                _pending_images.append((rel_path, refs))
        except Exception as e:
            logging.getLogger("memory").warning(
                "[memory] image-ref extraction %s failed: %s", rel_path, e
            )


async def _flush_memory(pending, pending_images=None):
    from memory import db, store
    if not pending and not pending_images:
        return
    if not db.is_enabled():
        return
    await db.init_pool()
    try:
        for rel, chunks, mtime in pending:
            try:
                await store.upsert_doc_chunks(rel, chunks, mtime)
            except Exception as e:
                logging.getLogger("memory").warning(
                    "[memory] upsert %s failed: %s", rel, e
                )
        # Markdown-referenced images: one MediaChunk per image, idempotent
        # via store.upsert_media_chunk's media_ref dedup. Requires an
        # embedder backend with image modality (e.g. gemini-2-preview).
        if pending_images:
            try:
                from memory.chunker_image import chunk_image
                from memory.embedders import get_backend
                backend = get_backend()
                if "image" not in backend.modalities:
                    logging.getLogger("memory").info(
                        "[memory] skipping md-image indexing: backend %r "
                        "has no image modality (set EMBED_BACKEND=gemini-2-preview)",
                        backend.name,
                    )
                else:
                    for rel_md, refs in pending_images:
                        for abs_img, alt in refs:
                            try:
                                caption = alt or abs_img.stem
                                chunk = chunk_image(abs_img, caption=caption)
                                await store.upsert_media_chunk(
                                    chunk,
                                    actor="indexer:md-image",
                                    context=f"from {rel_md}",
                                )
                            except Exception as e:
                                logging.getLogger("memory").warning(
                                    "[memory] md-image %s (from %s) failed: %s",
                                    abs_img, rel_md, e,
                                )
            except Exception as e:
                logging.getLogger("memory").warning(
                    "[memory] md-image flush failed: %s", e
                )
    finally:
        await db.close_pool()


async def _delete_all_doc_chunks():
    from memory import db
    if not db.is_enabled():
        return
    await db.init_pool()
    try:
        async with db.get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM memories WHERE kind='doc_chunk'")
            await conn.commit()
    finally:
        await db.close_pool()


def rebuild(conn):
    """Drop all indexed data and rebuild from scratch."""
    _pending_embeds.clear()
    _pending_images.clear()
    conn.executescript(
        "DELETE FROM pages; DELETE FROM pages_fts; "
        "DELETE FROM code_blocks; DELETE FROM code_blocks_fts;"
    )
    count = 0
    for abs_path in iter_doc_files(REPO_PATH):
        rel = abs_path.relative_to(REPO_PATH / CONTENT_SUBDIR).as_posix()
        upsert_page(conn, rel, abs_path)
        count += 1
    set_meta(conn, "last_commit_hash", current_commit())
    set_meta(conn, "last_indexed_at", str(int(time.time())))
    set_meta(conn, "repo_url", REPO_URL)
    conn.commit()

    if ENABLE_MEMORY_HOOK and (_pending_embeds or _pending_images):
        pending = list(_pending_embeds)
        pending_images = list(_pending_images)
        _pending_embeds.clear()
        _pending_images.clear()
        try:
            asyncio.run(_flush_memory(pending, pending_images))
        except Exception as e:
            logging.getLogger("memory").warning("[memory] flush failed: %s", e)
    return count


def incremental_update(conn):
    """Pull latest and re-index changed files only. Returns (added, updated, removed)."""
    _pending_embeds.clear()
    _pending_images.clear()
    prev_hash = get_meta(conn, "last_commit_hash")
    new_hash = current_commit()
    if prev_hash == new_hash:
        return (0, 0, 0)

    # In local mode we don't have git diff; do a full rebuild (cheap since
    # upsert_page is idempotent and mtime-sensitive). This also catches
    # deletions naturally.
    if DOCS_MODE == "local":
        count = rebuild(conn)
        return (count, 0, 0)

    # Determine changed files from git diff
    diff_out = _run(
        ["git", "-C", str(REPO_PATH), "diff", "--name-status", f"{prev_hash}..{new_hash}"],
        check=False,
    ) if prev_hash else ""

    added = updated = removed = 0

    if not prev_hash:
        # No previous index — full rebuild
        count = rebuild(conn)
        return (count, 0, 0)

    content_prefix = f"{CONTENT_SUBDIR}/"
    for line in diff_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1]
        if not path.startswith(content_prefix):
            continue
        if not (path.endswith(".md") or path.endswith(".mdx")):
            continue
        rel = path[len(content_prefix):]
        abs_path = REPO_PATH / path
        if status.startswith("D"):
            conn.execute("DELETE FROM pages WHERE path = ?", (rel,))
            conn.execute("DELETE FROM pages_fts WHERE path = ?", (rel,))
            conn.execute("DELETE FROM code_blocks WHERE path = ?", (rel,))
            conn.execute("DELETE FROM code_blocks_fts WHERE path = ?", (rel,))
            removed += 1
        else:
            existing = conn.execute("SELECT 1 FROM pages WHERE path = ?", (rel,)).fetchone()
            if abs_path.exists():
                upsert_page(conn, rel, abs_path)
                if existing:
                    updated += 1
                else:
                    added += 1

    set_meta(conn, "last_commit_hash", new_hash)
    set_meta(conn, "last_indexed_at", str(int(time.time())))
    conn.commit()

    if ENABLE_MEMORY_HOOK and (_pending_embeds or _pending_images):
        pending = list(_pending_embeds)
        pending_images = list(_pending_images)
        _pending_embeds.clear()
        _pending_images.clear()
        try:
            asyncio.run(_flush_memory(pending, pending_images))
        except Exception as e:
            logging.getLogger("memory").warning("[memory] flush failed: %s", e)
    return (added, updated, removed)


def print_stats(conn):
    n_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    n_code = conn.execute("SELECT COUNT(*) FROM code_blocks").fetchone()[0]
    last_hash = get_meta(conn, "last_commit_hash")
    last_at = get_meta(conn, "last_indexed_at")
    sections = conn.execute("SELECT section, COUNT(*) AS n FROM pages GROUP BY section").fetchall()
    print(f"pages:         {n_pages}")
    print(f"code blocks:   {n_code}")
    print(f"last commit:   {last_hash}")
    print(f"last indexed:  {last_at}")
    for s in sections:
        print(f"  section {s['section']}: {s['n']} pages")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="Drop and rebuild the full index")
    parser.add_argument("--update", action="store_true", help="Pull and incrementally update")
    parser.add_argument("--stats", action="store_true", help="Print index stats")
    parser.add_argument("--skip-git", action="store_true", help="Don't clone/pull (use existing repo)")
    parser.add_argument("--no-embed", action="store_true", help="Skip memory upsert even if MEMORY_ENABLED=true")
    parser.add_argument("--reembed-all", action="store_true", help="With --rebuild: wipe doc_chunk memories and re-embed everything")
    args = parser.parse_args()

    if args.no_embed:
        set_memory_hook(False)

    if args.stats:
        with open_db() as conn:
            print_stats(conn)
        return 0

    if args.reembed_all:
        if not args.rebuild:
            print("--reembed-all requires --rebuild", file=sys.stderr)
            return 2
        if os.environ.get("CONFIRM_REEMBED", "").lower() != "yes":
            print("--reembed-all requires CONFIRM_REEMBED=yes", file=sys.stderr)
            return 2
        if ENABLE_MEMORY_HOOK:
            try:
                asyncio.run(_delete_all_doc_chunks())
                print("[memory] deleted all doc_chunk rows")
            except Exception as e:
                print(f"[memory] failed to delete doc_chunk rows: {e}", file=sys.stderr)
                return 1

    if not args.skip_git:
        ensure_repo()

    conn = open_db()
    try:
        if args.rebuild:
            n = rebuild(conn)
            print(f"Rebuilt index: {n} pages")
        else:
            a, u, r = incremental_update(conn)
            print(f"Updated index: +{a} added, ~{u} updated, -{r} removed")
        print_stats(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
