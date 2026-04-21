"""Unified media reconciler.

Reconciles the media files under the docs root against the `memories`
table with add / update / delete semantics. Runs at boot (always) and
from the fs watcher (local mode) or on explicit tool invocation.

Two change-detection drivers:

  - DOCS_MODE="git": delegate to ``git diff --name-status prev..HEAD`` so
    we only touch files that actually changed between the last indexed
    commit and the current HEAD. State is tracked via the SQLite `meta`
    table under the key `last_media_commit_hash` (separate from the
    md/mdx pipeline's `last_commit_hash` so either can advance
    independently if one fails).

  - DOCS_MODE="local": walk the docs root, diff the filesystem set
    against `store.list_media_source_paths_with_hash()`. For each path
    check `mtime + size` first (cheap), fall back to SHA-256 only when
    the mtime/size signal suggests a change.

The caller is responsible for running this in an asyncio task (the
background ingest manager in ``ingest_task.py``). This module itself
must not spawn tasks; it's a pure coroutine returning a summary dict.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from . import db, store
from .embedders import get_backend
from .media_router import MEDIA_ROUTES, route_media

log = logging.getLogger("memory.reconcile")


# Meta-table keys. Kept separate from the md/mdx pipeline so a failure
# on one side does not silently "skip" the other on the next boot.
META_KEY_GIT_HASH = "last_media_commit_hash"


@dataclass
class ReconcileSummary:
    """Per-run outcome, surfaced in /health and logs."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "skipped": self.skipped,
            "errors": self.errors[-20:],  # cap for /health payload
            "by_kind": self.by_kind,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": (
                round(self.finished_at - self.started_at, 2)
                if self.finished_at else None
            ),
        }


ProgressCb = Callable[[str, dict], Awaitable[None]] | None


async def _emit(progress_cb: ProgressCb, phase: str, payload: dict) -> None:
    if progress_cb is None:
        return
    try:
        res = progress_cb(phase, payload)
        if asyncio.iscoroutine(res):
            await res
    except Exception as e:  # pragma: no cover — progress must never break the run
        log.debug("[reconcile] progress_cb %s raised %s", phase, e)


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Stream a file through sha256. Chunks of 1 MiB keep memory flat."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _iter_media_files(root: Path) -> Iterable[Path]:
    """Walk `root` yielding regular files with a supported media suffix.

    Skips anything starting with a dot (e.g. `.DS_Store`, `.git`,
    `.alephignore`) to keep OS noise out of the ingest loop. Returns
    paths in a stable sorted order for deterministic diff logs.
    """
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in MEDIA_ROUTES:
            yield p


# ---------------------------------------------------------------------------
# Upsert orchestration for a single file
# ---------------------------------------------------------------------------


async def _upsert_file(
    abs_path: Path, *, source_sha256: str, source_mtime: int, actor: str,
) -> dict:
    """Chunk one file and upsert every returned chunk.

    Returns a summary dict {kind: count} for telemetry.
    """
    # `route_media` is async now — video/audio chunkers may call ASR
    # (which is network-bound). For image/pdf the coroutine returns
    # almost immediately after the synchronous chunker call.
    chunks = await route_media(abs_path, caption=None)
    by_kind: dict[str, int] = {}
    src_str = str(abs_path.resolve())
    for c in chunks:
        try:
            res = await store.upsert_media_chunk(
                c,
                actor=actor,
                source_path=src_str,
                source_sha256=source_sha256,
                source_mtime=source_mtime,
            )
            by_kind[res.get("kind", c.kind)] = by_kind.get(
                res.get("kind", c.kind), 0
            ) + 1
        except Exception as e:
            log.warning(
                "[reconcile] upsert_media_chunk failed for %s (kind=%s): %s",
                abs_path, c.kind, e,
            )
            raise
    # Cleanup tempdirs stashed by route_media (video/audio/pdf derivatives).
    tmpdirs = {c.metadata.get("_tmpdir") for c in chunks if c.metadata.get("_tmpdir")}
    for td in tmpdirs:
        try:
            if td:
                shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass
    return by_kind


# ---------------------------------------------------------------------------
# Backend gate — media reconcile needs a multimodal embedder.
# ---------------------------------------------------------------------------


def _backend_supports_any_media() -> tuple[bool, str, list[str]]:
    """Return (ok, backend_name, supported_modalities) for diagnostics."""
    try:
        backend = get_backend()
        mods = sorted(set(backend.modalities) - {"text"})
        return (len(mods) > 0, backend.name, mods)
    except Exception as e:  # pragma: no cover
        return (False, f"<error: {e}>", [])


# ---------------------------------------------------------------------------
# Local mode: walk + hash diff
# ---------------------------------------------------------------------------


async def _reconcile_local(
    root: Path, progress_cb: ProgressCb,
) -> ReconcileSummary:
    summary = ReconcileSummary(started_at=time.time())

    await _emit(progress_cb, "scan", {"root": str(root)})
    fs_files: list[Path] = list(_iter_media_files(root))
    fs_meta: dict[str, tuple[int, int]] = {}  # path -> (mtime, size)
    for p in fs_files:
        try:
            stt = p.stat()
            fs_meta[str(p.resolve())] = (int(stt.st_mtime), int(stt.st_size))
        except FileNotFoundError:
            continue

    db_state = await store.list_media_source_paths_with_hash()
    await _emit(progress_cb, "diff", {
        "fs_count": len(fs_meta), "db_count": len(db_state),
    })

    to_add: list[str] = []
    to_update: list[str] = []
    to_remove: list[str] = [sp for sp in db_state.keys() if sp not in fs_meta]

    for sp, (mt, sz) in fs_meta.items():
        entry = db_state.get(sp)
        if entry is None:
            to_add.append(sp)
            continue
        db_mt_raw = entry.get("mtime")
        try:
            db_mt = int(db_mt_raw) if db_mt_raw is not None else None
        except (TypeError, ValueError):
            db_mt = None
        if db_mt is not None and db_mt == mt:
            summary.skipped += 1
            continue
        # mtime differs → hash the file to confirm content actually changed.
        to_update.append(sp)

    total_work = len(to_add) + len(to_update) + len(to_remove)
    await _emit(progress_cb, "plan", {
        "add": len(to_add), "update": len(to_update),
        "remove": len(to_remove), "skip": summary.skipped,
        "total": total_work,
    })

    # 1) Deletions first — cheap + frees up disambiguation if a path
    #    was replaced by a file with a different extension.
    for sp in to_remove:
        try:
            rows = await store.delete_by_source_path(
                sp, actor="reconcile:local"
            )
            summary.removed += len(rows)
        except Exception as e:
            msg = f"delete {sp}: {type(e).__name__}: {e}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)

    # 2) Adds and updates share the embed/upsert path. Skip if backend
    #    cannot handle media at all — surface as errors so /health is loud.
    ok, backend_name, mods = _backend_supports_any_media()
    if (to_add or to_update) and not ok:
        msg = (
            f"embedder backend {backend_name!r} has no media modalities "
            f"({mods or 'none'}); cannot ingest "
            f"{len(to_add) + len(to_update)} media file(s). "
            "Set EMBED_BACKEND=gemini-2-preview."
        )
        log.warning("[reconcile] %s", msg)
        summary.errors.append(msg)
        summary.finished_at = time.time()
        return summary

    done = 0

    async def _process(sp: str, kind_label: str) -> None:
        nonlocal done
        done += 1
        p = Path(sp)
        if not p.is_file():
            msg = f"{kind_label}: file vanished before ingest: {sp}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)
            return
        await _emit(progress_cb, kind_label, {
            "path": sp, "done": done, "total": total_work,
        })
        try:
            mt, sz = fs_meta[sp]
            # Hash is the authoritative source-of-change signal, computed
            # only when we've already decided to touch the file.
            sha = await asyncio.to_thread(_sha256_file, p)
            if kind_label == "update":
                db_sha = (db_state.get(sp) or {}).get("sha256")
                if db_sha and db_sha == sha:
                    # mtime changed but content identical; bump mtime via
                    # delete+insert would be wasteful. Skip.
                    summary.skipped += 1
                    return
                # Cascade-delete old chunks before re-ingesting.
                await store.delete_by_source_path(sp, actor="reconcile:update")
            by_kind = await _upsert_file(
                p, source_sha256=sha, source_mtime=mt, actor=f"reconcile:{kind_label}",
            )
            for k, n in by_kind.items():
                summary.by_kind[k] = summary.by_kind.get(k, 0) + n
            if kind_label == "add":
                summary.added += 1
            else:
                summary.updated += 1
        except Exception as e:
            msg = f"{kind_label} {sp}: {type(e).__name__}: {e}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)

    for sp in to_add:
        await _process(sp, "add")
    for sp in to_update:
        await _process(sp, "update")

    summary.finished_at = time.time()
    log.info(
        "[reconcile] local done: +%d ~%d -%d skip=%d errors=%d (%.1fs)",
        summary.added, summary.updated, summary.removed,
        summary.skipped, len(summary.errors),
        summary.finished_at - summary.started_at,
    )
    return summary


# ---------------------------------------------------------------------------
# Git mode: rely on `git diff --name-status` between commits
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def _read_meta(sqlite_conn: sqlite3.Connection, key: str) -> str | None:
    row = sqlite_conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    # sqlite row_factory may be Row; str-cast for portability.
    if row is None:
        return None
    try:
        return row["value"]
    except Exception:
        return str(row[0])


def _write_meta(sqlite_conn: sqlite3.Connection, key: str, value: str) -> None:
    sqlite_conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    sqlite_conn.commit()


async def _reconcile_git(
    repo_root: Path, content_sub: str,
    sqlite_conn: sqlite3.Connection,
    progress_cb: ProgressCb,
) -> ReconcileSummary:
    """Reconcile using git diff between last_media_commit_hash and HEAD.

    The content subdirectory is the same `CONTENT_SUBDIR` used by the
    md/mdx pipeline — in git mode this is typically "content". Media
    files SHOULD live inside that subtree too; files outside it are
    skipped (mirrors the md pipeline behaviour).
    """
    summary = ReconcileSummary(started_at=time.time())

    try:
        head = _run_git(["rev-parse", "HEAD"], repo_root)
    except Exception as e:
        msg = f"git rev-parse HEAD failed: {e}"
        log.warning("[reconcile] %s", msg)
        summary.errors.append(msg)
        summary.finished_at = time.time()
        return summary

    prev = _read_meta(sqlite_conn, META_KEY_GIT_HASH)

    # Decide the set of paths to process.
    added_paths: list[str] = []
    modified_paths: list[str] = []
    deleted_paths: list[str] = []
    full_scan = False

    if not prev:
        # First run: treat all media files in the worktree as adds.
        full_scan = True
    else:
        try:
            diff_out = _run_git(
                ["diff", "--name-status", f"{prev}..{head}"], repo_root,
            )
        except Exception as e:
            msg = f"git diff failed (falling back to full scan): {e}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)
            full_scan = True
        else:
            for line in diff_out.splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                status = parts[0]
                # Handle rename (Rxxx old\tnew) — count as delete+add.
                if status.startswith("R") and len(parts) >= 3:
                    old_rel, new_rel = parts[1], parts[2]
                    deleted_paths.append(old_rel)
                    modified_paths.append(new_rel)
                    continue
                rel = parts[-1]
                if status.startswith("D"):
                    deleted_paths.append(rel)
                elif status.startswith("A"):
                    added_paths.append(rel)
                else:  # M, T, C, etc.
                    modified_paths.append(rel)

    content_prefix = f"{content_sub}/" if content_sub else ""

    def _is_media_rel(rel: str) -> bool:
        if content_prefix and not rel.startswith(content_prefix):
            return False
        return Path(rel).suffix.lower() in MEDIA_ROUTES

    added_paths = [r for r in added_paths if _is_media_rel(r)]
    modified_paths = [r for r in modified_paths if _is_media_rel(r)]
    deleted_paths = [r for r in deleted_paths if _is_media_rel(r)]

    if full_scan:
        content_root = repo_root / content_sub if content_sub else repo_root
        for p in _iter_media_files(content_root):
            rel = str(p.relative_to(repo_root))
            added_paths.append(rel)

    await _emit(progress_cb, "plan", {
        "head": head, "prev": prev,
        "add": len(added_paths), "update": len(modified_paths),
        "remove": len(deleted_paths), "full_scan": full_scan,
    })

    # 1) Deletes — resolve to the same absolute path used during insert
    #    so `delete_by_source_path` matches.
    for rel in deleted_paths:
        abs_str = str((repo_root / rel).resolve())
        try:
            rows = await store.delete_by_source_path(
                abs_str, actor="reconcile:git",
            )
            summary.removed += len(rows)
        except Exception as e:
            msg = f"delete {rel}: {type(e).__name__}: {e}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)

    ok, backend_name, mods = _backend_supports_any_media()
    if (added_paths or modified_paths) and not ok:
        msg = (
            f"embedder backend {backend_name!r} has no media modalities "
            f"({mods or 'none'}); cannot ingest "
            f"{len(added_paths) + len(modified_paths)} media file(s). "
            "Set EMBED_BACKEND=gemini-2-preview."
        )
        log.warning("[reconcile] %s", msg)
        summary.errors.append(msg)
        summary.finished_at = time.time()
        return summary

    total_work = len(added_paths) + len(modified_paths)
    done = 0

    async def _process_git(rel: str, kind_label: str) -> None:
        nonlocal done
        done += 1
        p = (repo_root / rel).resolve()
        if not p.is_file():
            msg = f"{kind_label}: file missing in worktree: {rel}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)
            return
        await _emit(progress_cb, kind_label, {
            "path": str(p), "rel": rel, "done": done, "total": total_work,
        })
        try:
            stt = p.stat()
            sha = await asyncio.to_thread(_sha256_file, p)
            if kind_label == "update":
                await store.delete_by_source_path(
                    str(p), actor="reconcile:git-update",
                )
            by_kind = await _upsert_file(
                p, source_sha256=sha, source_mtime=int(stt.st_mtime),
                actor=f"reconcile:git-{kind_label}",
            )
            for k, n in by_kind.items():
                summary.by_kind[k] = summary.by_kind.get(k, 0) + n
            if kind_label == "add":
                summary.added += 1
            else:
                summary.updated += 1
        except Exception as e:
            msg = f"{kind_label} {rel}: {type(e).__name__}: {e}"
            log.warning("[reconcile] %s", msg)
            summary.errors.append(msg)

    for rel in added_paths:
        await _process_git(rel, "add")
    for rel in modified_paths:
        await _process_git(rel, "update")

    # Persist the new pointer ONLY on successful completion so a failure
    # mid-run results in a re-attempt rather than silent desync.
    if not summary.errors or len(summary.errors) < (
        len(added_paths) + len(modified_paths) + len(deleted_paths)
    ):
        # Partial success still advances the pointer — the errored files
        # stay as persistent diagnostics in `summary.errors`. Full failure
        # (every op errored) means we leave `prev` in place.
        try:
            _write_meta(sqlite_conn, META_KEY_GIT_HASH, head)
        except Exception as e:
            log.warning(
                "[reconcile] failed to persist %s=%s: %s",
                META_KEY_GIT_HASH, head, e,
            )

    summary.finished_at = time.time()
    log.info(
        "[reconcile] git done: +%d ~%d -%d skip=%d errors=%d "
        "prev=%s head=%s (%.1fs)",
        summary.added, summary.updated, summary.removed,
        summary.skipped, len(summary.errors),
        (prev or "<none>")[:8], head[:8],
        summary.finished_at - summary.started_at,
    )
    return summary


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def reconcile_media(
    *,
    mode: str | None = None,
    root: Path | None = None,
    repo_root: Path | None = None,
    content_sub: str | None = None,
    sqlite_conn_factory: Callable[[], sqlite3.Connection] | None = None,
    progress_cb: ProgressCb = None,
) -> ReconcileSummary:
    """Reconcile media in `root` (local mode) or `repo_root/content_sub` (git mode).

    Normally called without explicit args — the caller is the ingest
    task manager, which reads DOCS_MODE / REPO_PATH / CONTENT_SUBDIR
    from the indexer module and wires them in. The arg-rich signature
    exists so tests can drive the reconciler with arbitrary paths.
    """
    if not db.is_enabled():
        log.info("[reconcile] memory disabled — nothing to do")
        s = ReconcileSummary(started_at=time.time())
        s.finished_at = s.started_at
        return s

    # Lazy-resolve defaults from `indexer` so we honour env overrides
    # exactly the way the md/mdx pipeline does.
    if mode is None or root is None or repo_root is None or content_sub is None:
        import indexer  # type: ignore
        mode = mode or indexer.DOCS_MODE
        repo_root = repo_root or indexer.REPO_PATH
        content_sub = content_sub if content_sub is not None else indexer.CONTENT_SUBDIR
        root = root or (repo_root / content_sub if content_sub else repo_root)

    await db.init_pool()

    if mode == "git":
        if sqlite_conn_factory is None:
            import indexer  # type: ignore
            sqlite_conn_factory = indexer.open_db
        sqlite_conn = sqlite_conn_factory()
        try:
            return await _reconcile_git(
                repo_root=repo_root, content_sub=content_sub,
                sqlite_conn=sqlite_conn,
                progress_cb=progress_cb,
            )
        finally:
            try:
                sqlite_conn.close()
            except Exception:
                pass

    # Local mode
    return await _reconcile_local(root, progress_cb)


__all__ = ["reconcile_media", "ReconcileSummary", "META_KEY_GIT_HASH"]
