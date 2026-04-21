"""Shared media extension → chunker dispatch.

Lifted out of `tools/memory.py` so the on-demand `remember_media` tool
and the boot-time / watcher-driven reconciler use one code path. Keep
this module dependency-light: import chunkers lazily via string keys
and re-export `_MEDIA_ROUTES` so callers can also test membership
without triggering heavy imports.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List

from .types import MediaChunk


# Extension → modality label. Keep in sync with the per-modality
# chunker MIME allowlists.
MEDIA_ROUTES: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".pdf": "pdf",
}


def is_supported_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_ROUTES


def route_media(path: Path, *, caption: str | None = None) -> List[MediaChunk]:
    """Dispatch a media file to its chunker based on extension.

    Mirrors the behaviour previously in `tools/memory.py:_route_media`:
    image → single chunk; video/audio/pdf → one chunk per segment/page,
    with tempdirs stashed in metadata['_tmpdir'] so callers can reap
    them after all upserts.
    """
    suffix = path.suffix.lower()
    modality = MEDIA_ROUTES.get(suffix)
    if modality is None:
        raise ValueError(
            f"extension {suffix!r} is not a recognised media type. "
            f"Allowed: {sorted(MEDIA_ROUTES.keys())}"
        )

    if modality == "image":
        from .chunker_image import chunk_image
        return [chunk_image(path, caption=caption)]

    if modality == "video":
        from .chunker_video import chunk_video
        tmpdir = tempfile.mkdtemp(prefix="aleph-video-")
        chunks = chunk_video(path, out_dir=Path(tmpdir), caption=caption)
        if not chunks:
            raise RuntimeError(f"chunk_video returned no scenes for {path}")
        for c in chunks:
            c.metadata.setdefault("_tmpdir", tmpdir)
        return chunks

    if modality == "audio":
        from .chunker_audio import chunk_audio
        tmpdir = tempfile.mkdtemp(prefix="aleph-audio-")
        chunks = chunk_audio(path, out_dir=Path(tmpdir), transcript=caption)
        if not chunks:
            raise RuntimeError(f"chunk_audio returned no clips for {path}")
        for c in chunks:
            c.metadata.setdefault("_tmpdir", tmpdir)
        return chunks

    if modality == "pdf":
        from .chunker_pdf import chunk_pdf
        chunks = chunk_pdf(path)
        if not chunks:
            raise RuntimeError(f"chunk_pdf returned no pages for {path}")
        return chunks

    raise ValueError(f"unhandled modality: {modality}")


__all__ = ["MEDIA_ROUTES", "is_supported_media", "route_media"]
