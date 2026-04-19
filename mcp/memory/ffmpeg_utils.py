"""ffmpeg / ffprobe helpers.

Requires ``ffmpeg`` and ``ffprobe`` in ``$PATH`` — check with
:func:`check_ffmpeg` at startup, raises :class:`FFmpegMissing` otherwise.

All subprocess calls use ``check=True``, ``capture_output=True``,
``text=True`` and a 120 s timeout. ``shell=True`` is never used.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger("memory")

_TIMEOUT_S = 120

# Gemini 2 preview hard caps (see PRD §5.4).
VIDEO_SEGMENT_MAX_S = 120.0
AUDIO_SEGMENT_MAX_S = 80.0


class FFmpegMissing(RuntimeError):
    """Raised when ffmpeg or ffprobe is not on $PATH."""


def check_ffmpeg() -> None:
    """Verify both ``ffmpeg`` and ``ffprobe`` are discoverable on $PATH.

    Raises :class:`FFmpegMissing` with an operator-friendly message if
    either is missing.
    """
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise FFmpegMissing(
            f"ffmpeg tools not found on $PATH: {', '.join(missing)}. "
            "Install with `apt install -y ffmpeg` (Linux) or `brew install ffmpeg` (macOS)."
        )


def _run(cmd: list[str], *, timeout: int = _TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run a subprocess with the conventions used across this module."""
    logger.debug("ffmpeg_utils._run: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        # Surface stderr in the message — ffmpeg writes almost everything there.
        raise RuntimeError(
            f"{cmd[0]} failed (exit {e.returncode}): {e.stderr.strip()[:2000]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{cmd[0]} timed out after {timeout}s: {' '.join(cmd)}"
        ) from e


def probe(path: Path) -> dict:
    """Run ``ffprobe`` and return a condensed dict.

    Returns ``{duration_s, codec, resolution, streams[...]}`` where
    ``codec`` is the primary video codec (or audio codec for pure audio
    files) and ``resolution`` is ``"WxH"`` or ``None``.
    """
    check_ffmpeg()
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = _run(cmd).stdout
    data = json.loads(out or "{}")

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    # Duration: prefer format, fall back to first stream duration.
    duration_s: float | None = None
    if fmt.get("duration") is not None:
        try:
            duration_s = float(fmt["duration"])
        except (TypeError, ValueError):
            duration_s = None
    if duration_s is None:
        for s in streams:
            if s.get("duration"):
                try:
                    duration_s = float(s["duration"])
                    break
                except (TypeError, ValueError):
                    pass

    # Primary codec + resolution.
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    primary = video or audio
    codec = (primary or {}).get("codec_name")

    resolution: str | None = None
    if video and video.get("width") and video.get("height"):
        resolution = f"{int(video['width'])}x{int(video['height'])}"

    return {
        "duration_s": duration_s,
        "codec": codec,
        "resolution": resolution,
        "streams": streams,
    }


def extract_keyframes(
    path: Path,
    out_dir: Path,
    scene_threshold: float = 0.4,
) -> List[Tuple[float, Path]]:
    """Scene-based keyframe extraction.

    Uses ``ffmpeg -vf select='gt(scene,T)'`` with ``showinfo`` to capture
    per-frame timestamps. Returns ``[(t_seconds, png_path), ...]``.

    Falls back to fixed-interval (every 10 s) if the scene detector
    produces fewer than 2 frames.
    """
    check_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Scene-detect with showinfo so we can parse timestamps from stderr.
    scene_pattern = str(out_dir / "scene_%04d.png")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", str(path),
        "-vf", f"select='gt(scene,{scene_threshold})',showinfo",
        "-vsync", "vfr",
        "-frame_pts", "true",
        scene_pattern,
    ]
    proc = _run(cmd)

    # Parse `pts_time:<float>` from stderr (showinfo output).
    import re

    times: list[float] = []
    for m in re.finditer(r"pts_time:(\d+\.?\d*)", proc.stderr or ""):
        try:
            times.append(float(m.group(1)))
        except ValueError:
            continue

    frames = sorted(out_dir.glob("scene_*.png"))
    # Pair file with time by order (ffmpeg emits them in order).
    pairs: list[tuple[float, Path]] = []
    for i, f in enumerate(frames):
        t = times[i] if i < len(times) else 0.0
        pairs.append((t, f))

    if len(pairs) >= 2:
        return pairs

    # Fallback: fixed 10s interval.
    logger.info(
        "ffmpeg_utils.extract_keyframes: scene-detect found %d frame(s); "
        "falling back to 10s fixed interval for %s",
        len(pairs), path,
    )
    # Clean up any partial frames from scene-detect so numbering is fresh.
    for f in frames:
        try:
            f.unlink()
        except OSError:
            pass

    info = probe(path)
    duration = info.get("duration_s") or 0.0
    if duration <= 0.0:
        # Can't grid-sample without a duration; return at least t=0 if we got one.
        return pairs

    fallback_pattern = str(out_dir / "kf_%04d.png")
    cmd2 = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", str(path),
        "-vf", "fps=1/10",
        fallback_pattern,
    ]
    _run(cmd2)

    out_frames = sorted(out_dir.glob("kf_*.png"))
    out_pairs: list[tuple[float, Path]] = []
    for i, f in enumerate(out_frames):
        out_pairs.append((float(i * 10), f))
    return out_pairs


def extract_video_segment(
    path: Path,
    t_start: float,
    t_end: float,
    out_path: Path,
) -> Path:
    """Extract ``[t_start, t_end]`` with stream copy (fast, no re-encode).

    Caps the segment length at :data:`VIDEO_SEGMENT_MAX_S` even if the
    caller asks for more (Gemini 2 video limit).
    """
    check_ffmpeg()
    if t_end <= t_start:
        raise ValueError(f"extract_video_segment: t_end <= t_start ({t_start} -> {t_end})")
    length = min(t_end - t_start, VIDEO_SEGMENT_MAX_S)
    t_end_capped = t_start + length

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss", f"{t_start:.3f}",
        "-to", f"{t_end_capped:.3f}",
        "-i", str(path),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(out_path),
    ]
    _run(cmd)
    return out_path


def segment_audio(
    path: Path,
    out_dir: Path,
    window_s: float = 80.0,
    overlap_s: float = 2.0,
) -> List[Tuple[float, float, Path]]:
    """Produce overlapping WAV segments.

    Returns ``[(t_start, t_end, path), ...]``. ``window_s`` is capped to
    :data:`AUDIO_SEGMENT_MAX_S` (Gemini 2 audio limit).
    """
    check_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)

    window_s = min(float(window_s), AUDIO_SEGMENT_MAX_S)
    overlap_s = max(0.0, float(overlap_s))
    if overlap_s >= window_s:
        raise ValueError(
            f"segment_audio: overlap_s ({overlap_s}) must be < window_s ({window_s})"
        )

    info = probe(path)
    duration = info.get("duration_s") or 0.0
    if duration <= 0.0:
        raise ValueError(f"segment_audio: could not determine duration of {path}")

    step = window_s - overlap_s
    segments: list[tuple[float, float, Path]] = []
    t = 0.0
    idx = 0
    while t < duration - 0.05:  # skip micro-tail segments
        t_end = min(t + window_s, duration)
        seg_path = out_dir / f"audio_{idx:04d}.wav"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss", f"{t:.3f}",
            "-to", f"{t_end:.3f}",
            "-i", str(path),
            "-vn",
            "-c:a", "pcm_s16le",
            str(seg_path),
        ]
        _run(cmd)
        segments.append((t, t_end, seg_path))
        idx += 1
        if t_end >= duration:
            break
        t += step

    return segments


__all__ = [
    "FFmpegMissing",
    "VIDEO_SEGMENT_MAX_S",
    "AUDIO_SEGMENT_MAX_S",
    "check_ffmpeg",
    "probe",
    "extract_keyframes",
    "extract_video_segment",
    "segment_audio",
]
