"""Video → paired MediaChunks per scene (video + transcript).

Per scene we emit:
  - one ``video_scene`` chunk whose embedding is computed from the
    segment .mp4 by the multimodal backend (existing behaviour)
  - optionally, one ``video_transcript`` chunk whose ``content`` is
    the Whisper transcript of the same segment and whose embedding
    is computed as TEXT (cheap, works with any backend)

The transcript chunk is only emitted when ASR actually returns text.
When the ASR backend is disabled, unreachable, or the segment is
silent, we silently fall back to the legacy "scene N @ Xs" content
on the video_scene row with zero transcript row — so ingest never
fails because of ASR.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from . import asr, ffmpeg_utils, media
from .types import MediaChunk

logger = logging.getLogger("memory")


_VIDEO_MIMES = {"video/mp4", "video/quicktime"}


def _pick_segment_bounds(scene_times: list[float], duration: float) -> list[tuple[float, float]]:
    """Pair each scene start with the next scene's start (or file end).

    Caps segment length to ``VIDEO_SEGMENT_MAX_S``. Returns non-empty list
    (fallbacks to a single whole-file segment).
    """
    if not scene_times or duration <= 0.0:
        if duration > 0.0:
            end = min(duration, ffmpeg_utils.VIDEO_SEGMENT_MAX_S)
            return [(0.0, end)]
        # Unknown duration — take a single max-cap window starting at 0.
        return [(0.0, ffmpeg_utils.VIDEO_SEGMENT_MAX_S)]

    sorted_ts = sorted(set(t for t in scene_times if t >= 0.0))
    if sorted_ts[0] > 0.0:
        sorted_ts.insert(0, 0.0)

    bounds: list[tuple[float, float]] = []
    for i, t_start in enumerate(sorted_ts):
        t_end = sorted_ts[i + 1] if i + 1 < len(sorted_ts) else duration
        # Cap segment length.
        t_end = min(t_end, t_start + ffmpeg_utils.VIDEO_SEGMENT_MAX_S)
        if t_end - t_start < 0.05:
            continue
        bounds.append((t_start, t_end))
    return bounds or [(0.0, min(duration, ffmpeg_utils.VIDEO_SEGMENT_MAX_S))]


async def chunk_video(
    path: Path,
    out_dir: Path,
    caption: str | None = None,
) -> List[MediaChunk]:
    """Probe, keyframe, segment; return one or two MediaChunks per scene.

    Each scene yields a ``video_scene`` chunk (whose ``path`` is the
    segment .mp4 file in ``out_dir`` — the caller-provided tempdir
    must outlive the embedding call). When the ASR backend produces
    a non-empty transcript for that segment, an additional
    ``video_transcript`` chunk is emitted with ``content=transcript``
    and ``path=None`` — the store embeds the transcript string as
    text rather than passing a file to the multimodal backend.

    Args:
        path: Absolute path to the source video.
        out_dir: Directory (typically a TemporaryDirectory) to hold
            segment .mp4 files and keyframe PNGs. MUST outlive the
            embedding call.
        caption: Optional caption; used verbatim as ``content`` for
            scenes where ASR returned no text. When ASR produced a
            transcript, it takes precedence.
    """
    if not path.is_file():
        raise FileNotFoundError(f"chunk_video: not a file: {path}")

    mime = media.detect_mime(path)
    if mime not in _VIDEO_MIMES:
        raise ValueError(
            f"chunk_video: {path} has MIME {mime!r}, expected one of {sorted(_VIDEO_MIMES)}"
        )

    ffmpeg_utils.check_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = ffmpeg_utils.probe(path)
    duration = float(info.get("duration_s") or 0.0)
    codec = info.get("codec")
    src_sha = media.sha256_file(path)

    # Extract keyframes (may fall back to fixed interval).
    kf_dir = out_dir / "keyframes"
    keyframes = ffmpeg_utils.extract_keyframes(path, kf_dir)
    scene_times = [t for t, _ in keyframes]
    kf_paths = [p for _, p in keyframes]

    bounds = _pick_segment_bounds(scene_times, duration)

    # If fewer keyframes than bounds (fallback scenario w/ unknown scenes),
    # use kf[0] for every segment; else pair positionally.
    def _kf_for(i: int) -> Path | None:
        if not kf_paths:
            return None
        return kf_paths[i] if i < len(kf_paths) else kf_paths[-1]

    chunks: list[MediaChunk] = []
    resolved_src = str(path.resolve())
    asr_lang = os.environ.get("ASR_LANGUAGE", "").strip() or None
    # When false (zero-cost text-only mode), skip the expensive
    # video_scene row. The scene .mp4 segment is still produced so
    # Whisper can transcribe it — we just never send it to the
    # multimodal embedder.
    hybrid = os.environ.get("HYBRID_MEDIA_EMBEDDING", "true").strip().lower() == "true"

    for i, (t_start, t_end) in enumerate(bounds):
        seg_path = out_dir / f"scene_{i:04d}{path.suffix.lower()}"
        try:
            ffmpeg_utils.extract_video_segment(path, t_start, t_end, seg_path)
        except Exception as e:
            logger.warning(
                "chunk_video: failed to extract segment %d (%.2fs-%.2fs) from %s: %s",
                i, t_start, t_end, path, e,
            )
            continue

        thumb: str | None = None
        kf = _kf_for(i)
        if kf and kf.is_file():
            try:
                thumb = media.make_image_thumbnail(kf)
            except Exception as e:
                logger.warning(
                    "chunk_video: thumbnail failed for scene %d keyframe %s: %s",
                    i, kf, e,
                )

        # Transcribe the segment BEFORE deciding what content to use.
        # asr.transcribe never raises — empty string signals disabled
        # or silent, in which case we fall back to caption/placeholder.
        raw_transcript = (await asr.transcribe(seg_path, language=asr_lang)).strip()
        # Filter out low-signal Whisper output ("." from silent scenes,
        # "[Music]", single-letter words) that would cluster as garbage
        # in the embedding space.
        transcript = raw_transcript if media.is_meaningful_text(raw_transcript) else ""

        if transcript:
            content = transcript
        elif caption and caption.strip():
            content = caption.strip()
        else:
            content = f"scene {i} @ {t_start:.1f}s"

        meta = {
            "sha256_src": src_sha,
            "t_start_s": round(t_start, 3),
            "t_end_s": round(t_end, 3),
            "duration_s": round(t_end - t_start, 3),
            "codec": codec,
            "scene_idx": i,
        }
        if transcript:
            meta["has_transcript"] = True

        if hybrid:
            chunks.append(MediaChunk(
                kind="video_scene",
                content=content,
                media_ref=f"{resolved_src}#t={t_start:.2f}",
                media_type=mime,
                preview_b64=thumb,
                metadata=meta,
                path=seg_path,
            ))

        # Paired text-embedded row — only when ASR actually produced
        # text. It shares source_path + media_ref + thumbnail with the
        # video row so the reconciler's cascade-delete removes both
        # together on file update/deletion, and the viewer anchors
        # both dots to the same scene.
        if transcript:
            chunks.append(MediaChunk(
                kind="video_transcript",
                content=transcript,
                media_ref=f"{resolved_src}#t={t_start:.2f}",
                media_type="text/plain",
                preview_b64=thumb,
                metadata={
                    **meta,
                    "transcript_source": asr.get_backend().name,
                },
                path=None,
            ))

    if not chunks:
        # Pathological: produce a single whole-file fallback segment.
        t_end = min(duration or ffmpeg_utils.VIDEO_SEGMENT_MAX_S,
                    ffmpeg_utils.VIDEO_SEGMENT_MAX_S)
        seg_path = out_dir / f"scene_0000{path.suffix.lower()}"
        ffmpeg_utils.extract_video_segment(path, 0.0, t_end, seg_path)
        _raw_fb = (await asr.transcribe(seg_path, language=asr_lang)).strip()
        fallback_transcript = _raw_fb if media.is_meaningful_text(_raw_fb) else ""
        if fallback_transcript:
            content = fallback_transcript
        elif caption and caption.strip():
            content = caption.strip()
        else:
            content = "scene 0 @ 0.0s"
        fallback_meta = {
            "sha256_src": src_sha,
            "t_start_s": 0.0,
            "t_end_s": round(t_end, 3),
            "duration_s": round(t_end, 3),
            "codec": codec,
            "scene_idx": 0,
        }
        if fallback_transcript:
            fallback_meta["has_transcript"] = True
        if hybrid:
            chunks.append(MediaChunk(
                kind="video_scene",
                content=content,
                media_ref=f"{resolved_src}#t=0.00",
                media_type=mime,
                preview_b64=None,
                metadata=fallback_meta,
                path=seg_path,
            ))
        if fallback_transcript:
            chunks.append(MediaChunk(
                kind="video_transcript",
                content=fallback_transcript,
                media_ref=f"{resolved_src}#t=0.00",
                media_type="text/plain",
                preview_b64=None,
                metadata={
                    **fallback_meta,
                    "transcript_source": asr.get_backend().name,
                },
                path=None,
            ))

    logger.debug(
        "chunk_video: %s -> %d scene(s) duration=%.2fs codec=%s",
        path.name, len(chunks), duration, codec,
    )
    return chunks


__all__ = ["chunk_video"]
