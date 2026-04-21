"""Audio → paired MediaChunks per ≤80 s overlapping segment.

Per segment we emit:
  - one ``audio_clip`` chunk whose embedding is the multimodal audio
    vector (requires a backend with audio modality, e.g.
    gemini-2-preview)
  - optionally, one ``audio_transcript`` chunk whose ``content`` is
    the Whisper transcript — text-embedded, so works with every
    backend including `local` (Ollama).

When ASR is disabled or the segment is silent, only the audio_clip
row is emitted with a placeholder content — so ingest never fails
because of ASR.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from . import asr, ffmpeg_utils, media
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


async def chunk_audio(
    path: Path,
    out_dir: Path,
    transcript: str | None = None,
) -> List[MediaChunk]:
    """Window audio into ≤80 s segments and optionally ASR-transcribe each.

    Args:
        path: Absolute path to the source audio file (mp3 or wav).
        out_dir: Directory (typically a TemporaryDirectory) to hold
            per-segment WAV files. MUST outlive the embedding call.
        transcript: Optional caller-provided full-file transcript.
            When set and the active ASR backend is disabled, it is
            sliced (character-proportional) across segments. When
            ASR is active, per-segment Whisper output takes
            precedence over this argument.
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
    asr_lang = os.environ.get("ASR_LANGUAGE", "").strip() or None
    # Zero-cost text-only mode: keep only the audio_transcript row.
    hybrid = os.environ.get("HYBRID_MEDIA_EMBEDDING", "true").strip().lower() == "true"

    chunks: list[MediaChunk] = []
    for i, (t_start, t_end, seg_path) in enumerate(segments):
        # Per-segment Whisper (if active). Swallows all errors → "".
        _raw = (await asr.transcribe(seg_path, language=asr_lang)).strip()
        # Drop low-signal junk ("." from silence, "[Music]" cues, etc.)
        # so they don't pollute the embedding space as a garbage cluster.
        seg_transcript = _raw if media.is_meaningful_text(_raw) else ""

        # Pick content: Whisper > caller-provided sliced transcript > placeholder.
        if seg_transcript:
            content = seg_transcript
        elif transcript:
            content = (
                _slice_transcript(transcript, n, i)
                or f"clip {i} @ {t_start:.1f}s"
            )
        else:
            content = f"clip {i} @ {t_start:.1f}s"

        meta = {
            "sha256_src": src_sha,
            "t_start_s": round(t_start, 3),
            "t_end_s": round(t_end, 3),
            "duration_s": round(t_end - t_start, 3),
            "codec": codec,
        }
        if seg_transcript:
            meta["has_transcript"] = True

        if hybrid:
            chunks.append(MediaChunk(
                kind="audio_clip",
                content=content,
                media_ref=f"{resolved_src}#t={t_start:.2f},{t_end:.2f}",
                media_type=mime,
                preview_b64=None,  # v1: no waveform thumbnail; viewer renders on demand.
                metadata=meta,
                path=seg_path,
            ))

        if seg_transcript:
            chunks.append(MediaChunk(
                kind="audio_transcript",
                content=seg_transcript,
                media_ref=f"{resolved_src}#t={t_start:.2f},{t_end:.2f}",
                media_type="text/plain",
                preview_b64=None,
                metadata={
                    **meta,
                    "transcript_source": asr.get_backend().name,
                },
                path=None,
            ))

    logger.debug(
        "chunk_audio: %s -> %d clip(s) duration=%.2fs codec=%s",
        path.name, len(chunks), duration, codec,
    )
    return chunks


__all__ = ["chunk_audio"]
