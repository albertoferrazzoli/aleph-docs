"""Shared dataclasses for media chunkers.

Kept in a standalone module so `chunker_image`, `chunker_video`,
`chunker_audio`, `chunker_pdf` (owned by separate Wave 2 agents) all
import the same `MediaChunk` without cycling through each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MediaChunk:
    """One media memory row awaiting embedding + insert.

    Attributes:
        kind: One of "image" | "video_scene" | "audio_clip" | "pdf_page".
        content: Caption (if provided) or filename stem; this is what
            surfaces in the audit trail and UI lists.
        media_ref: Absolute path (images/audio/video) or "file.pdf#page=N".
        media_type: MIME string (e.g. "image/png").
        preview_b64: Base64 thumbnail (no data: prefix), ≤ 20 KB, or None.
        metadata: Free-form dict — {sha256, w, h, bytes, t_start_s, ...}.
        path: The actual filesystem Path handed to the embedder backend
            via `backend.embed([chunk.path], ...)`. For kinds that embed
            a derived clip (video scenes), this is the clip file, not
            the source.
    """

    kind: str
    content: str
    media_ref: str
    media_type: str
    preview_b64: str | None
    metadata: dict = field(default_factory=dict)
    path: Path | None = None


__all__ = ["MediaChunk"]
