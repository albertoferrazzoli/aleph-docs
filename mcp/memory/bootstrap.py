"""One-shot embedding bootstrap for the semantic memory.

Iterates all markdown/mdx files under $DOCS_REPO_PATH/content/, chunks them,
batch-embeds via Gemini, and upserts into the `memories` table. Idempotent:
`store.upsert_doc_chunks` skips chunks whose content-hash is unchanged.

Usage:
    python -m memory.bootstrap [--limit N] [--content-dir PATH] [--reembed-all]

The cost estimate below is a rough guide — update the constant if Gemini
pricing changes.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from indexer import parse_frontmatter, extract_headings, REPO_PATH, CONTENT_SUBDIR  # noqa: E402
from memory import chunker, db, embeddings, store  # noqa: E402

# Gemini text-embedding pricing (USD per 1k tokens). Update as needed.
GEMINI_USD_PER_1K_TOKENS = 0.00013

log = logging.getLogger("memory")


def _iter_md(root: Path):
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".mdx"):
            yield p


async def _run(args) -> int:
    if not db.is_enabled():
        log.error("[memory] disabled — set MEMORY_ENABLED=true and PG_DSN")
        return 2
    if not os.getenv("GOOGLE_API_KEY"):
        log.error("[memory] GOOGLE_API_KEY not set — cannot embed")
        return 2

    await db.init_pool()
    try:
        if args.reembed_all:
            if os.getenv("CONFIRM_REEMBED") != "yes":
                log.error(
                    "[memory] refusing --reembed-all without CONFIRM_REEMBED=yes"
                )
                return 3
            async with db.get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM memories WHERE kind='doc_chunk'")
                await conn.commit()
            log.warning(
                "[memory] deleted all doc_chunk rows — re-embedding from scratch"
            )

        content_root = (
            Path(args.content_dir).resolve()
            if args.content_dir
            else REPO_PATH / CONTENT_SUBDIR
        )
        files = list(_iter_md(content_root))
        if args.limit:
            files = files[: args.limit]
        log.info(
            "[memory] scanning %d markdown files under %s", len(files), content_root
        )

        totals = {"inserted": 0, "updated": 0, "skipped": 0, "deleted": 0, "chunks": 0}
        embeddings.reset_token_counter()
        start = time.time()

        for i, abs_path in enumerate(files, 1):
            try:
                rel = str(abs_path.relative_to(content_root))
            except ValueError:
                rel = str(abs_path)
            try:
                raw = abs_path.read_text(encoding="utf-8", errors="replace")
                fm, body = parse_frontmatter(raw)
                headings = extract_headings(body)
                chunks = chunker.chunk(rel, body, fm, headings)
            except Exception as e:
                log.exception("[memory] failed to parse %s: %s", rel, e)
                continue
            if not chunks:
                continue
            mtime = int(abs_path.stat().st_mtime)
            try:
                res = await store.upsert_doc_chunks(rel, chunks, mtime)
            except Exception as e:
                log.exception("[memory] upsert failed for %s: %s", rel, e)
                continue
            totals["chunks"] += len(chunks)
            for k in ("inserted", "updated", "skipped", "deleted"):
                totals[k] += res.get(k, 0) if isinstance(res, dict) else 0
            if i % 10 == 0 or i == len(files):
                log.info(
                    "[memory] [%d/%d] %s | ins=%d upd=%d skip=%d del=%d",
                    i,
                    len(files),
                    rel,
                    totals["inserted"],
                    totals["updated"],
                    totals["skipped"],
                    totals["deleted"],
                )

        elapsed = time.time() - start
        tokens = embeddings.tokens_used()
        cost = (tokens / 1000.0) * GEMINI_USD_PER_1K_TOKENS
        try:
            counts = await store.count_by_kind()
        except Exception:
            counts = {}

        print(f"\n[memory] done in {elapsed:.1f}s")
        print(f"  files:            {len(files)}")
        print(f"  chunks emitted:   {totals['chunks']}")
        print(
            f"  ins/upd/skip/del: "
            f"{totals['inserted']}/{totals['updated']}/{totals['skipped']}/{totals['deleted']}"
        )
        print(f"  est tokens:       ~{tokens:,}")
        print(f"  est cost (USD):   ~${cost:.4f}  (@ ${GEMINI_USD_PER_1K_TOKENS}/1k)")
        print(f"  memories table:   {counts}")
        return 0
    finally:
        await db.close_pool()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="Embed all docs into the semantic memory (one-shot bootstrap)."
    )
    ap.add_argument(
        "--reembed-all",
        action="store_true",
        help="Delete all existing doc_chunk rows before re-embedding. "
        "Requires env CONFIRM_REEMBED=yes.",
    )
    ap.add_argument(
        "--limit", type=int, help="Process only the first N files (for dry-runs)."
    )
    ap.add_argument(
        "--content-dir",
        type=str,
        help="Override content root (default: $DOCS_REPO_PATH/content).",
    )
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
