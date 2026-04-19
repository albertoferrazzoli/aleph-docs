"""Tests for memory.chunker_video and memory.ffmpeg_utils (video side).

Gated on ffmpeg presence — skipped entirely on machines without it.
Fixtures synthesize tiny videos at test time via ffmpeg's lavfi source.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_video(path: Path, duration: int, rate: int = 10, size: str = "320x240") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def mp4_file(tmp_path):
    return _make_video(tmp_path / "test.mp4", duration=5)


@pytest.fixture
def mp4_long(tmp_path):
    # 200s so we can assert the 120s cap.
    return _make_video(tmp_path / "long.mp4", duration=200, rate=5, size="160x120")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ffmpeg_available():
    """Sanity: skip-guard fired iff ffmpeg really is here."""
    from memory import ffmpeg_utils

    ffmpeg_utils.check_ffmpeg()  # must not raise


def test_probe_mp4(mp4_file):
    from memory import ffmpeg_utils

    info = ffmpeg_utils.probe(mp4_file)
    assert info["duration_s"] is not None
    assert 4.0 <= info["duration_s"] <= 6.0
    assert info["codec"]  # non-empty string
    assert info["resolution"] == "320x240"


def test_chunk_video_produces_segments(mp4_file, tmp_path):
    from memory import chunker_video

    out_dir = tmp_path / "segments"
    chunks = chunker_video.chunk_video(mp4_file, out_dir=out_dir)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.kind == "video_scene"
        assert c.media_type in {"video/mp4", "video/quicktime"}
        assert c.path is not None and c.path.is_file()
        assert c.media_ref.startswith(str(mp4_file.resolve()))
        assert "#t=" in c.media_ref
        assert "sha256_src" in c.metadata
        assert c.metadata["t_start_s"] >= 0.0
        assert c.metadata["t_end_s"] > c.metadata["t_start_s"]
        assert c.metadata["scene_idx"] == chunks.index(c)


def test_chunk_video_caps_at_120s(mp4_long, tmp_path):
    from memory import chunker_video

    out_dir = tmp_path / "segments"
    chunks = chunker_video.chunk_video(mp4_long, out_dir=out_dir)
    assert len(chunks) >= 1
    for c in chunks:
        dur = c.metadata["t_end_s"] - c.metadata["t_start_s"]
        assert dur <= 120.0 + 1e-3, f"segment {c.metadata} exceeded 120s cap"
