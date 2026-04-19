"""Narrow lookup helpers on top of `search_docs`.

These wrap the FTS5 search with path-prefix / section filters so the LLM can
reach the right information with a single tool call. Adapt the helpers to
your own doc structure — they are intentionally generic examples.
"""

import os

from helpers import db_conn, error_response, fts_escape


# Optional env vars to scope the built-in helpers. Leave empty to search
# across the whole documentation.
CLI_SUBTREES     = [s for s in os.environ.get("CLI_SUBTREES", "").split(",") if s]
ERROR_SECTION    = os.environ.get("ERROR_SECTION", "").strip() or None
API_SECTION      = os.environ.get("API_SECTION", "").strip() or None


def _search_scoped(query: str, path_prefix: str | None = None,
                   section: str | None = None,
                   fts_table: str = "pages_fts", limit: int = 10) -> list[dict]:
    match = fts_escape(query)
    sql = f"""
        SELECT path, title, section,
               snippet({fts_table}, 2, '**', '**', ' … ', 20) AS snippet,
               bm25({fts_table}) AS rank
        FROM {fts_table}
        WHERE {fts_table} MATCH ?
    """
    params: list = [match]
    if section:
        sql += " AND section = ?"
        params.append(section)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    with db_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    if path_prefix:
        results = [r for r in results if r["path"].startswith(path_prefix)]
    return results


def register(mcp):
    @mcp.tool()
    def find_command_line_option(flag: str, limit: int = 10) -> dict:
        """Search documentation for a CLI flag or argument.

        Strips leading dashes if present. Scope configurable via the
        CLI_SUBTREES env var (comma-separated path prefixes); if unset,
        searches the whole docs.

        Args:
            flag: Flag name or keyword (e.g. '--verbose').
            limit: Max results (default 10).
        """
        try:
            q = flag.lstrip("-")
            if not CLI_SUBTREES:
                results = _search_scoped(q, limit=limit)
            else:
                results = []
                for prefix in CLI_SUBTREES:
                    results.extend(_search_scoped(q, path_prefix=prefix, limit=limit))
            seen, out = set(), []
            for r in results:
                if r["path"] not in seen:
                    seen.add(r["path"])
                    out.append(r)
            return {"flag": flag, "count": len(out[:limit]), "results": out[:limit]}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def find_error_message(text: str, limit: int = 10) -> dict:
        """Search documentation for an error message, code or exception.

        Use when a user quotes an error — this locates the relevant
        troubleshooting section. Optionally scoped via ERROR_SECTION env.

        Args:
            text: Full or partial error message / code / exception name.
            limit: Max results.
        """
        try:
            results = _search_scoped(text, section=ERROR_SECTION, limit=limit)
            return {"query": text, "count": len(results), "results": results}
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def find_api_endpoint(query: str, limit: int = 10) -> dict:
        """Search API documentation for an endpoint / schema / field.

        Optionally scoped via API_SECTION env (e.g. 'api'); if unset,
        searches the whole docs.

        Args:
            query: Endpoint path, operation name, or field name.
            limit: Max results.
        """
        try:
            results = _search_scoped(query, section=API_SECTION, limit=limit)
            return {"query": query, "count": len(results), "results": results}
        except Exception as e:
            return error_response(e)
