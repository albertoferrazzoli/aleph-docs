"""Search tools for Aleph Docs MCP."""

from helpers import db_conn, error_response, fts_escape

try:
    from memory.reinforce import record_interaction
except ImportError:
    def record_interaction(_tool_name):
        return lambda fn: fn


def register(mcp):
    @mcp.tool()
    @record_interaction("search_docs")
    def search_docs(query: str, section: str | None = None, limit: int = 10) -> dict:
        """Keyword (lexical) search across MARKDOWN PAGES ONLY.

        ⚠ SCOPE: This searches the `pages_fts` SQLite FTS5 index, which
        covers *only* `.md`/`.mdx` files under `docs/`. It does NOT see
        video transcripts, audio transcripts, PDF page text, or image
        chunks — those live in the memories vector store and are
        reachable only via `semantic_search` (text + multimodal) or
        `search_images` (visual).

        When to use THIS tool:
          • the user asks for exact keywords / flag names in docs
          • the user asks which markdown page documents X
          • you need BM25-ranked prose snippets with highlights

        When to use `semantic_search` INSTEAD:
          • the corpus includes video courses, recorded meetings,
            screencasts, audio notes, or PDFs (Whisper transcripts and
            PDF page text are only indexed there)
          • the user asks conceptual questions ("what does the
            instructor say about X?", "summarize the course")
          • you want results ranked by meaning, not token overlap

        Returning zero or few hits here is a strong signal to retry
        with `semantic_search` before telling the user the corpus is
        empty — especially for course / tutorial content.

        Args:
            query: Search text. Multi-word queries are AND-combined.
            section: Optional section filter ('guides', 'reference', 'api').
            limit: Max number of results (default 10, max 50).
        """
        try:
            limit = max(1, min(int(limit), 50))
            match = fts_escape(query)
            where = ""
            params: list = [match]
            if section:
                where = "AND section = ?"
                params.append(section)
            sql = f"""
                SELECT path, title, section,
                       snippet(pages_fts, 2, '**', '**', ' … ', 20) AS snippet,
                       bm25(pages_fts) AS rank
                FROM pages_fts
                WHERE pages_fts MATCH ? {where}
                ORDER BY rank
                LIMIT ?
            """
            params.append(limit)
            with db_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return {
                "query": query,
                "section": section,
                "count": len(rows),
                "results": [dict(r) for r in rows],
            }
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    @record_interaction("search_code_examples")
    def search_code_examples(query: str, language: str | None = None, limit: int = 10) -> dict:
        """Search only within fenced code blocks across the documentation.

        Useful to find example snippets (e.g. 'MyApp MSBuild task').
        Optionally filter by language tag ('csharp', 'xml', 'bash', 'json', ...).

        Args:
            query: Search text matched against code block content.
            language: Optional language tag filter.
            limit: Max results (default 10, max 50).
        """
        try:
            limit = max(1, min(int(limit), 50))
            match = fts_escape(query)
            sql = """
                SELECT path, language,
                       snippet(code_blocks_fts, 0, '**', '**', ' … ', 20) AS snippet,
                       bm25(code_blocks_fts) AS rank
                FROM code_blocks_fts
                WHERE code_blocks_fts MATCH ?
            """
            params: list = [match]
            if language:
                sql += " AND language = ?"
                params.append(language.lower())
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            with db_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return {
                "query": query,
                "language": language,
                "count": len(rows),
                "results": [dict(r) for r in rows],
            }
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    @record_interaction("find_related")
    def find_related(path: str, limit: int = 5) -> dict:
        """Find pages related to the given one (same section, overlapping terms).

        Uses the page's title as an FTS query restricted to its section.

        Args:
            path: Page path (e.g. 'guides/introduction/index.md').
            limit: Max results (default 5, max 20).
        """
        try:
            limit = max(1, min(int(limit), 20))
            with db_conn() as conn:
                page = conn.execute(
                    "SELECT title, section FROM pages WHERE path = ?", (path,)
                ).fetchone()
                if not page:
                    return {"error": f"Page not found: {path}"}
                match = fts_escape(page["title"])
                rows = conn.execute(
                    """
                    SELECT path, title, section,
                           snippet(pages_fts, 2, '**', '**', ' … ', 15) AS snippet,
                           bm25(pages_fts) AS rank
                    FROM pages_fts
                    WHERE pages_fts MATCH ? AND section = ? AND path != ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match, page["section"], path, limit),
                ).fetchall()
            return {
                "path": path,
                "title": page["title"],
                "count": len(rows),
                "results": [dict(r) for r in rows],
            }
        except Exception as e:
            return error_response(e)
