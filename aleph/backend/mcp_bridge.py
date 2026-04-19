"""Thin wrappers around memory.store for Aleph endpoints."""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import db

log = logging.getLogger("aleph")


async def search(query: str, kind: Optional[str] = None,
                 limit: int = 10, min_score: float = 0.15) -> list[dict]:
    return await db.store.search(
        query=query, kind=kind, limit=limit, min_score=min_score,
    )


async def remember(content: str, context: str = "",
                   source_path: Optional[str] = None,
                   tags: Optional[list[str]] = None) -> dict:
    return await db.store.insert_insight(
        content=content, context=context or "",
        source_path=source_path, tags=tags or [],
    )


async def forget(memory_id: str) -> dict:
    return await db.store.forget(memory_id)


async def node_detail(node_id: str) -> Optional[dict]:
    node = await db.get_node(node_id)
    if node is None:
        return None
    neighbors = await db.get_neighbors(node_id, k=8, min_w=0.4)
    return {"node": node, "neighbors": neighbors}
