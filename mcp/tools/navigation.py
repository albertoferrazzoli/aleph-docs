"""Navigation tools: sections, page trees, page lists."""

import fnmatch

from helpers import db_conn, error_response


def register(mcp):
    @mcp.tool()
    def list_sections() -> dict:
        """List top-level documentation sections with their page counts.

        Typical sections: 'guides', 'reference', 'api' (adapt to your own sections).
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
        """Return a hierarchical tree of pages within a section.

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
        """List pages as a flat list, optionally filtered by section and glob pattern.

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
