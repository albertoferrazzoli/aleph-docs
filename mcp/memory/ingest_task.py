"""Singleton task manager for the media reconciler.

Serialises reconcile runs (no two in flight at once), exposes live
progress for `/health`, and is safe to call from multiple sources
(startup hook, filesystem watcher, MCP tool).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import reconcile

log = logging.getLogger("memory.ingest_task")


@dataclass
class _Progress:
    state: str = "idle"  # idle | running
    phase: str = ""       # scan | diff | plan | add | update | delete
    current_path: str = ""
    processed: int = 0
    total: int = 0
    started_at: float = 0.0
    last_summary: dict = field(default_factory=dict)


class IngestTask:
    """Module-level singleton. Access via :func:`get_ingest_task`."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._progress = _Progress()

    def snapshot(self) -> dict:
        p = self._progress
        return {
            "state": p.state,
            "phase": p.phase,
            "current": p.current_path,
            "processed": p.processed,
            "total": p.total,
            "started_at": p.started_at,
            "last_summary": p.last_summary or None,
        }

    async def _progress_cb(self, phase: str, payload: dict) -> None:
        self._progress.phase = phase
        if phase in ("add", "update", "delete"):
            self._progress.current_path = str(payload.get("path", ""))
            if "done" in payload:
                self._progress.processed = int(payload["done"])
            if "total" in payload:
                self._progress.total = int(payload["total"])
        elif phase == "plan":
            total = 0
            for k in ("add", "update", "remove"):
                total += int(payload.get(k, 0))
            self._progress.total = total
            self._progress.processed = 0

    async def run_once(self, **kwargs) -> reconcile.ReconcileSummary:
        """Kick off a reconcile run. Serialised via an internal lock."""
        if self._lock.locked():
            log.info("[ingest] run_once called while a run is in flight — waiting")
        async with self._lock:
            self._progress = _Progress(
                state="running",
                started_at=time.time(),
            )
            try:
                summary = await reconcile.reconcile_media(
                    progress_cb=self._progress_cb, **kwargs
                )
            except Exception as e:
                log.exception("[ingest] run_once crashed: %s", e)
                self._progress.state = "idle"
                self._progress.phase = "error"
                self._progress.last_summary = {"error": f"{type(e).__name__}: {e}"}
                raise
            self._progress.last_summary = summary.as_dict()
            self._progress.state = "idle"
            self._progress.phase = "done"
            return summary


_singleton: Optional[IngestTask] = None


def get_ingest_task() -> IngestTask:
    global _singleton
    if _singleton is None:
        _singleton = IngestTask()
    return _singleton


__all__ = ["IngestTask", "get_ingest_task"]
