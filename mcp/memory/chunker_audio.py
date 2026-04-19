"""Audio → one MediaChunk per ≤80 s overlapping segment.

No embedding happens here; that is deferred to
``store.upsert_media_chunk``. The chunker splits the file via ffmpeg
into overlapping windows and hands each segment file to the caller
embedded in a :class:`MediaChunk` (with ``path`` pointing at it).

Note: segment files live in ``out_dir`` which MUST outlive the
embedding call — the backend reads them during ``.embed([path])``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from . import ffmpeg_utils, media
from .types import MediaChunk

logger = logging.getLogger("memory")


_AUDIO_MIMES = {"audio/mpeg", "audio/wav"}


def _slice_transcript(transcript: str, n: int, i: int) -> str:
    """Very rough even-chunk slice of a transcript.

    We have no word-level timestamps here, so we just partition the
    transcript into ``n`` near-equal slices by character count. Good
    enough to feed per-segment ``content``; callers with real STT
    timings should pre-split and pass ``transcript=None`` plus custom
    content via the store layer.
    """
    if not transcript or n <= 0:
        return ""
    L = len(transcript)
    if L == 0:
        return ""
    size = max(1, L // n)
    start = i * size
    end = L if i == n - 1 else start + size
    return transcript[start:end].strip()


def chunk_audio(
    path: Path,
    out_dir: Path,
    transcript: str | None = None,
) -> List[MediaChunk]:
    """Window audio into ≤80 s segments (2 s overlap).

    Args:
        path: Absolute path to the source audio file (mp3 or wav).
        out_dir: Directory (typically a TemporaryDirectory) to hold
            per-segment WAV files. MUST outlive the embedding call.
        transcript: Optional full-file transcript. If provided, per
            segment ``content`` gets a proportional slice; else
            ``content`` is ``f"clip {i} @ {t_start:.1f}s"``.
    """
    if not path.is_file():
        raise FileNotFoundError(f"chunk_audio: not a file: {path}")

    mime = media.detect_mime(path)
    if mime not in _AUDIO_MIMES:
        raise ValueError(
            f"chunk_audio: {path} has MIME {mime!r}, expected one of {sorted(_AUDIO_MIMES)}"
        )

    ffmpeg_utils.check_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = ffmpeg_utils.probe(path)
    duration = float(info.get("duration_s") or 0.0)
    codec = info.get("codec")
    src_sha = media.sha256_file(path)

    segments = ffmpeg_utils.segment_audio(path, out_dir=out_dir)
    n = len(segments)
    resolved_src = str(path.resolve())

    chunks: list[MediaChunk] = []
    for i, (t_start, t_end, seg_path) in enumerate(segments):
        if transcript:
            content = _slice_transcript(transcript, n, i) or f"clip {i} @ {t_start:.1f}s"
        else:
            content = f"clip {i} @ {t_start:.1f}s"

        meta = {
            "sha256_src": src_sha,
            "t_start_s": round(t_start, 3),
            "t_end_s": round(t_end, 3),
            "duration_s": round(t_end - t_start, 3),
            "codec": codec,
        }

        chunks.append(MediaChunk(
            kind="audio_clip",
            content=content,
            media_ref=f"{resolved_src}#t={t_start:.2f},{t_end:.2f}",
            media_type=mime,
            preview_b64=None,  # v1: no waveform thumbnail; viewer renders on demand.
            metadata=meta,
            path=seg_path,
        ))

    logger.debug(
        "chunk_audio: %s -> %d clip(s) duration=%.2fs codec=%s",
        path.name, len(chunks), duration, codec,
    )
    return chunks


__all__ = ["chunk_audio"]
