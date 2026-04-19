"""Content tools: full page, page section, TOC, code blocks."""

import json
import re

from helpers import db_conn, error_response, row_to_dict


def register(mcp):
    @mcp.tool()
    def get_page(path: str) -> dict:
        """Return the full content and metadata of a documentation page.

        Args:
            path: Page path (e.g. 'guides/introduction/index.md').
        """
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT path, section, title, frontmatter, content, headings "
                    "FROM pages WHERE path = ?",
                    (path,),
                ).fetchone()
            if not row:
                return {"error": f"Page not found: {path}"}
            return row_to_dict(row)
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def get_table_of_contents(path: str) -> dict:
        """Return the list of headings (levels 1-6) for a page.

        Args:
            path: Page path.
        """
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT title, headings FROM pages WHERE path = ?", (path,)
                ).fetchone()
            if not row:
                return {"error": f"Page not found: {path}"}
            return {
                "path": path,
                "title": row["title"],
                "headings": json.loads(row["headings"]),
            }
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def get_page_section(path: str, heading: str) -> dict:
        """Return the content under a specific heading within a page.

        Matches the heading text case-insensitively. Returns content from that
        heading until the next heading of same or higher level (or end of page).

        Args:
            path: Page path.
            heading: Heading text to locate (case-insensitive).
        """
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT content FROM pages WHERE path = ?", (path,)
                ).fetchone()
            if not row:
                return {"error": f"Page not found: {path}"}
            body: str = row["content"]
            target = heading.lower().strip()
            lines = body.splitlines()
            start = None
            start_level = None
            heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
            for i, line in enumerate(lines):
                m = heading_re.match(line)
                if m and m.group(2).strip().lower() == target:
                    start = i
                    start_level = len(m.group(1))
                    break
            if start is None:
                return {"error": f"Heading not found: {heading}", "path": path}
            end = len(lines)
            for j in range(start + 1, len(lines)):
                m = heading_re.match(lines[j])
                if m and len(m.group(1)) <= start_level:
                    end = j
                    break
            section_text = "\n".join(lines[start:end]).strip()
            return {
                "path": path,
                "heading": heading,
                "level": start_level,
                "content": section_text,
            }
        except Exception as e:
            return error_response(e)

    @mcp.tool()
    def get_code_blocks(path: str, language: str | None = None) -> dict:
        """Return all fenced code blocks from a page, optionally filtered by language.

        Args:
            path: Page path.
            language: Optional language tag filter ('csharp', 'xml', 'bash', ...).
        """
        try:
            sql = "SELECT language, content FROM code_blocks WHERE path = ?"
            params: list = [path]
            if language:
                sql += " AND language = ?"
                params.append(language.lower())
            with db_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return {
                "path": path,
                "language": language,
                "count": len(rows),
                "blocks": [dict(r) for r in rows],
            }
        except Exception as e:
            return error_response(e)
