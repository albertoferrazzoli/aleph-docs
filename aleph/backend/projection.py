"""Build a graph snapshot from the `memories` table.

Run with:
    python -m backend.projection        (cwd = aleph/)

Steps:
    1. Fetch all rows (id, embedding, kind, ...).
    2. UMAP → 3D coords.
    3. HDBSCAN clusters on UMAP output.
    4. Top-k cosine neighbors via pgvector (one SQL query).
    5. Build payload {nodes, edges, generated_at, stats}.
    6. Insert into graph_snapshot with monotonic version + prune old.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import db

log = logging.getLogger("aleph")


SELECT_ROWS_SQL = """
SELECT id, embedding, kind::text, source_path, source_section, content,
       metadata, created_at, last_access_at, access_count, stability,
       media_ref, media_type, preview_b64
FROM memories
"""

TOP_K_SQL = """
WITH pairs AS (
  SELECT a.id AS a_id, b.id AS b_id, 1 - (a.embedding <=> b.embedding) AS w
  FROM memories a
  CROSS JOIN LATERAL (
    SELECT id, embedding FROM memories m
    WHERE m.id <> a.id
    ORDER BY a.embedding <=> m.embedding
    LIMIT 6
  ) b
)
SELECT a_id, b_id, w FROM pairs WHERE w > 0.25
"""


def _to_numpy(emb: Any) -> np.ndarray:
    """pgvector values arrive as lists or numpy arrays depending on adapter."""
    if isinstance(emb, np.ndarray):
        return emb.astype(np.float32)
    return np.asarray(emb, dtype=np.float32)


async def _fetch_rows() -> list[tuple]:
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(SELECT_ROWS_SQL)
            return await cur.fetchall()


async def _fetch_edges() -> list[tuple]:
    async with db.get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(TOP_K_SQL)
            return await cur.fetchall()


def _project_and_cluster(embeddings: np.ndarray) -> tuple[np.ndarray, list[int | None]]:
    """UMAP → xyz, HDBSCAN → cluster labels (None for noise)."""
    n = embeddings.shape[0]

    # import lazily so the rest of the backend works without umap installed
    import umap  # type: ignore
    import hdbscan  # type: ignore

    # Domain-uniform corpora (single-course transcripts, single-topic docs)
    # collapse into a dense blob with min_dist=0.35 because intra-cluster
    # cosines are ~0.5-0.7. Higher min_dist + lower n_neighbors opens up
    # local structure without losing the global topology.
    n_neighbors = max(2, min(15, n - 1))
    reducer = umap.UMAP(
        n_components=3,
        metric="cosine",
        random_state=42,
        min_dist=0.6,
        spread=1.5,
        n_neighbors=n_neighbors,
    )
    xyz = reducer.fit_transform(embeddings)

    # Center on the centroid, then normalize so the max absolute coordinate
    # lands at TARGET_EXTENT. The prototype's Three.js camera looks at origin
    # and is tuned for positions in ~[-80, 80]; raw UMAP is ~[-10, 10] and
    # often not centered, so 700+ halos collapse into a blob off-axis.
    TARGET_EXTENT = 80.0
    xyz = xyz - xyz.mean(axis=0, keepdims=True)
    max_abs = float(np.max(np.abs(xyz))) or 1.0
    xyz = xyz * (TARGET_EXTENT / max_abs)

    min_cs = max(2, min(8, n - 1))
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cs, metric="euclidean")
    raw_labels = clusterer.fit_predict(xyz)
    labels: list[int | None] = [
        (int(x) if x != -1 else None) for x in raw_labels
    ]
    return xyz.astype(float), labels


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


async def build_snapshot() -> dict:
    rows = await _fetch_rows()

    if len(rows) < 5:
        log.warning("[aleph] only %d memories — writing empty snapshot", len(rows))
        empty = {
            "nodes": [],
            "edges": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {"n_nodes": 0, "n_edges": 0, "n_clusters": 0},
        }
        return empty

    embeddings = np.vstack([_to_numpy(r[1]) for r in rows])
    ids = [str(r[0]) for r in rows]

    xyz, labels = _project_and_cluster(embeddings)

    nodes: list[dict] = []
    for i, r in enumerate(rows):
        (rid, _emb, kind, sp, ss, content, meta, created, last_access,
         ac, stab, media_ref, media_type, preview_b64) = r
        x, y, z = (float(xyz[i, 0]), float(xyz[i, 1]), float(xyz[i, 2]))
        # guard against NaN/inf — would blow up JSON
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            x = y = z = 0.0
        nodes.append({
            "id": str(rid),
            "kind": kind,
            "content": content,
            "source_path": sp,
            "source_section": ss,
            "metadata": meta or {},
            "embedding_3d": [x, y, z],
            "cluster": labels[i],
            "created_at": _iso(created),
            "last_access_at": _iso(last_access),
            "access_count": int(ac),
            "stability": float(stab),
            "media_ref": media_ref,
            "media_type": media_type,
            "preview_b64": preview_b64,
        })

    # Top-k edges — dedupe so each pair appears once (a<b).
    raw_edges = await _fetch_edges()
    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []
    for a_id, b_id, w in raw_edges:
        a, b = str(a_id), str(b_id)
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"a": key[0], "b": key[1], "w": float(w)})

    n_clusters = len({lbl for lbl in labels if lbl is not None})
    payload = {
        "nodes": nodes,
        "edges": edges,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "n_nodes": len(nodes),
            "n_edges": len(edges),
            "n_clusters": n_clusters,
        },
    }
    return payload


async def main() -> int:
    t0 = time.monotonic()
    # Only manage the pool lifecycle if nobody else did.
    owns_pool = db._mem_db._pool is None  # type: ignore[attr-defined]
    if owns_pool:
        await db.init_pool()
    try:
        payload = await build_snapshot()
        version = await db.insert_snapshot(payload)
    finally:
        if owns_pool:
            await db.close_pool()
    dur = time.monotonic() - t0
    stats = payload.get("stats", {})
    print(
        f"[aleph] snapshot v{version} written in {dur:.2f}s — "
        f"nodes={stats.get('n_nodes')} edges={stats.get('n_edges')} "
        f"clusters={stats.get('n_clusters')}"
    )
    return version


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(main())
