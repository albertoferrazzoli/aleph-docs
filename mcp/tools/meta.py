"""Meta tools: index stats and recent changes."""

import os

from helpers import db_conn, error_response


def register(mcp):
    @mcp.tool()
    def get_doc_stats() -> dict:
        """⚠ MARKDOWN ONLY — stats for the Markdown docs index.

        Returns counts from the SQLite `pages` / `code_blocks` tables.
        Does NOT include videos, audio, PDFs or images — those live in
        the pgvector memories store. For a full corpus breakdown by
        memory kind (video_transcript, image, pdf_text, …) call
        `memory_stats` instead.
        """
        try:
            with db_conn() as conn:
                n_pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                n_code = conn.execute("SELECT COUNT(*) FROM code_blocks").fetchone()[0]
                sections = [
                    dict(r) for r in conn.execute(
                        "SELECT section, COUNT(*) AS page_count "
                        "FROM pages GROUP BY section ORDER BY section"
                    ).fetchall()
                ]
                meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
                meta = {r["key"]: r["value"] for r in meta_rows}
            return {
                "pages": n_pages,
                "code_blocks": n_code,
                "sections": sections,
                "last_commit_hash": meta.get("last_commit_hash"),
                "last_indexed_at": meta.get("last_indexed_at"),
                "repo_url": meta.get("repo_url"),
            }
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def get_changelog(since_commit: str | None = None, limit: int = 20) -> dict:
        """Return recent commits on the docs repo (optionally since a given commit).

        Useful to see what changed in the documentation over time.

        Args:
            since_commit: Optional commit hash to diff from.
            limit: Max commits to return (default 20, max 100).
        """
        try:
            limit = max(1, min(int(limit), 100))
            # Import here so importing `tools.meta` doesn't require dotenv loaded.
            from indexer import git_log
            commits = git_log(since_commit=since_commit, limit=limit)
            return {"since": since_commit, "count": len(commits), "commits": commits}
        except Exception as e:
            return error_response(e)
