"""Filesystem watcher — local mode only.

Watches the docs root for additions / modifications / removals and
triggers the media reconciler via the ingest task singleton. Debounces
events so a batch of closely-spaced changes (e.g. `cp -r some-course/`)
results in a single reconcile run, not one per file.

Gating: in `DOCS_MODE="git"` :func:`start` is a no-op — the git clone
worktree is managed by the indexer itself and events there do not
reflect user intent. See the `project_change_detection_modes` memory
for the rationale.

The underlying library is `watchdog` (cross-platform; uses inotify on
Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows). When
running inside a Linux container watching a bind-mounted directory
from a macOS host, Docker Desktop forwards inotify events — no
polling fallback is needed in the common case.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from .media_router import MEDIA_ROUTES

log = logging.getLogger("memory.watcher")


_DEBOUNCE_SECONDS = 2.0


class DocsWatcher:
    """Single-root docs watcher. Not a singleton — start/stop are explicit."""

    def __init__(
        self,
        root: Path,
        ingest_task,  # IngestTask (avoid circular import at type-check time)
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._root = root
        self._task = ingest_task
        self._loop = loop
        self._observer = None  # watchdog Observer, lazy
        self._debounce_handle: Optional[asyncio.TimerHandle] = None

    def start(self) -> None:
        """Create the Observer, schedule handlers, spin the watcher thread."""
        try:
            from watchdog.events import PatternMatchingEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning(
                "[watcher] watchdog not installed — filesystem watcher disabled"
            )
            return

        if not self._root.is_dir():
            log.warning("[watcher] root does not exist: %s", self._root)
            return

        patterns = [f"*{ext}" for ext in MEDIA_ROUTES.keys()]
        patterns += ["*.md", "*.mdx"]  # md pipeline will pick these up; a
        # reconcile round-trip still benefits from a triggered re-scan.

        watcher = self  # captured for handler

        class _Handler(PatternMatchingEventHandler):
            def on_any_event(self, event):  # type: ignore[override]
                if event.is_directory:
                    return
                # Ignore dotfiles (.DS_Store, editor swap files).
                name = Path(event.src_path).name
                if name.startswith("."):
                    return
                watcher._schedule_debounced()

        handler = _Handler(
            patterns=patterns, ignore_directories=True, case_sensitive=False,
        )
        observer = Observer()
        observer.schedule(handler, str(self._root), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        log.info(
            "[watcher] started on %s (patterns=%s)",
            self._root, len(patterns),
        )

    def _schedule_debounced(self) -> None:
        """Called from the watchdog thread — marshal onto the event loop."""
        try:
            self._loop.call_soon_threadsafe(self._arm_timer)
        except RuntimeError:
            # Event loop already closed (during shutdown race) — silently drop.
            pass

    def _arm_timer(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_handle = self._loop.call_later(
            _DEBOUNCE_SECONDS, self._fire,
        )

    def _fire(self) -> None:
        self._debounce_handle = None
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            summary = await self._task.run_once()
            log.info(
                "[watcher] debounced reconcile done: +%d ~%d -%d skip=%d",
                summary.added, summary.updated, summary.removed, summary.skipped,
            )
        except Exception as e:
            log.exception("[watcher] reconcile failed: %s", e)

    def stop(self) -> None:
        if self._debounce_handle is not None:
            try:
                self._debounce_handle.cancel()
            except Exception:
                pass
            self._debounce_handle = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as e:  # pragma: no cover
                log.warning("[watcher] stop failed: %s", e)
            finally:
                self._observer = None
            log.info("[watcher] stopped")


def start_if_local(
    root: Path, ingest_task, loop: asyncio.AbstractEventLoop,
) -> Optional[DocsWatcher]:
    """Start watcher only when DOCS_MODE='local'. Returns None in git mode."""
    try:
        import indexer  # type: ignore
        mode = indexer.DOCS_MODE
    except Exception:
        mode = "local"
    if mode != "local":
        log.info(
            "[watcher] DOCS_MODE=%s — watcher disabled (git drives change detection)",
            mode,
        )
        return None
    w = DocsWatcher(root, ingest_task, loop)
    w.start()
    return w


__all__ = ["DocsWatcher", "start_if_local"]
