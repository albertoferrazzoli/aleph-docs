"""Semantic memory package for the aleph-docs-mcp MCP server.

See PRD_SEMANTIC_MEMORY.md for design. This package is intentionally lazy: it
does not perform any I/O or pool initialization at import time — callers must
explicitly invoke :func:`memory.db.init_pool` (typically from the MCP server
lifespan).
"""

from . import db  # re-export for convenience

__all__ = ["db"]
