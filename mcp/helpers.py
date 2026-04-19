"""Shared helpers for Aleph Docs MCP tools."""

import json
import os
import sqlite3
from contextlib import contextmanager


def get_db_path() -> str:
    return os.environ.get("DOCS_DB_PATH", "data/index.db")


@contextmanager
def db_conn():
    path = get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def error_response(exc: Exception) -> dict:
    return {"error": str(exc), "type": type(exc).__name__}


def row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if k in ("frontmatter", "headings") and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:
                pass
    return d


def fts_escape(query: str) -> str:
    """Escape a user query for FTS5 MATCH.

    Wraps each whitespace-separated token in double quotes so that special
    characters (hyphens, slashes, dots) don't break the query syntax.
    """
    tokens = [t for t in query.split() if t.strip()]
    if not tokens:
        return '""'
    escaped = []
    for t in tokens:
        t = t.replace('"', '""')
        escaped.append(f'"{t}"')
    return " ".join(escaped)
