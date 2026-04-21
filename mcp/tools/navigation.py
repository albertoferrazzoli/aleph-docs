"""Navigation tools: sections, page trees, page lists.

SCOPE: every tool in this module queries the SQLite `pages` table,
which only indexes Markdown files (`.md`/`.mdx`) under the docs root.
Video transcripts, audio transcripts, PDF text and images live in the
pgvector `memories` table and are NOT visible here. For a full corpus
overview across all 10 memory kinds use `memory_stats`. For content
retrieval across every kind use `search`.
"""

import fnmatch

from helpers import db_conn, error_response


def register(mcp):
    @mcp.tool()
    def list_sections() -> dict:
        """⚠ MARKDOWN ONLY — list top-level Markdown sections.

        Returns sections of the `pages` (Markdown) index. This will
        under-count corpora that include videos, audio or PDFs. To
        see the full corpus composition across every memory kind
        call `memory_stats` instead. For content lookup across any
        kind use `search`.
        """
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT section, COUNT(*) AS page_count "
                    "FROM pages GROUP BY section ORDER BY section"
                ).fetchall()
            return {"sections": [dict(r) for r in rows]}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def get_page_tree(section: str) -> dict:
        """⚠ MARKDOWN ONLY — hierarchical tree of Markdown pages.

        Video / audio / PDF content is not represented here. Use
        `memory_stats` for the full corpus breakdown.

        Args:
            section: Section name ('guides', 'reference', 'api').
        """
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT path, title FROM pages WHERE section = ? ORDER BY path",
                    (section,),
                ).fetchall()
            tree: dict = {}
            for r in rows:
                parts = r["path"].split("/")
                node = tree
                for part in parts[:-1]:
                    node = node.setdefault(part, {"_pages": []})
                node.setdefault("_pages", []).append({"path": r["path"], "title": r["title"]})
            return {"section": section, "tree": tree, "count": len(rows)}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def list_pages(section: str | None = None, pattern: str | None = None) -> dict:
        """⚠ MARKDOWN ONLY — flat list of Markdown pages.

        This is *not* a corpus overview — it sees only the Markdown
        index, so a zero / small result does not mean the corpus is
        empty. For "what is in this corpus?" call `memory_stats`.
        For content retrieval use `search`.

        Args:
            section: Optional section filter.
            pattern: Optional glob pattern matched against the page path
                     (e.g. 'guides/examples/*').
        """
        try:
            sql = "SELECT path, title, section FROM pages"
            params: list = []
            if section:
                sql += " WHERE section = ?"
                params.append(section)
            sql += " ORDER BY path"
            with db_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            result = [dict(r) for r in rows]
            if pattern:
                result = [r for r in result if fnmatch.fnmatch(r["path"], pattern)]
            return {"count": len(result), "pages": result}
        except Exception as e:
            return error_response(e)
