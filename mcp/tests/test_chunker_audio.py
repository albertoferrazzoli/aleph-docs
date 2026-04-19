"""Tests for memory.chunker_audio and memory.ffmpeg_utils (audio side).

Gated on ffmpeg presence — skipped entirely on machines without it.
Fixtures synthesize tiny WAVs at test time via ffmpeg's lavfi ``anullsrc``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on $PATH",
)


def _make_wav(path: Path, duration: int) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"anullsrc=r=16000:cl=mono",
            "-t", str(duration),
            "-c:a", "pcm_s16le",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def wav_short(tmp_path):
    return _make_wav(tmp_path / "short.wav", duration=10)


@pytest.fixture
def wav_long(tmp_path):
    return _make_wav(tmp_path / "long.wav", duration=200)


def test_chunk_audio_short_file(wav_short, tmp_path):
    from memory import chunker_audio

    out_dir = tmp_path / "segments"
    chunks = chunker_audio.chunk_audio(wav_short, out_dir=out_dir)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.kind == "audio_clip"
    assert c.media_type == "audio/wav"
    assert c.preview_b64 is None
    assert c.path is not None and c.path.is_file()
    assert c.metadata["t_start_s"] == 0.0
    assert 9.0 <= c.metadata["t_end_s"] <= 11.0


def test_chunk_audio_windows(wav_long, tmp_path):
    """200s silent wav -> 3 segments (80 + 80 + 40s), ≤2s overlap."""
    from memory import chunker_audio

    out_dir = tmp_path / "segments"
    chunks = chunker_audio.chunk_audio(wav_long, out_dir=out_dir)

    # With window=80, overlap=2 -> step=78. Starts: 0, 78, 156.
    # End of segment 3 = min(156+80, 200) = 200 -> stop.
    assert len(chunks) == 3

    # Monotonic starts, increasing
    starts = [c.metadata["t_start_s"] for c in chunks]
    assert starts == sorted(starts)

    # Each window ≤ 80s
    for c in chunks:
        dur = c.metadata["t_end_s"] - c.metadata["t_start_s"]
        assert dur <= 80.0 + 1e-3

    # Overlap between consecutive segments is ≤ 2s.
    for a, b in zip(chunks, chunks[1:]):
        overlap = a.metadata["t_end_s"] - b.metadata["t_start_s"]
        assert 0.0 <= overlap <= 2.0 + 1e-3

    # media_ref uses #t=start,end fragment
    for c in chunks:
        assert "#t=" in c.media_ref and "," in c.media_ref.split("#t=")[1]
