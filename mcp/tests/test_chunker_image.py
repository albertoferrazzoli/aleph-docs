"""Unit tests for memory.chunker_image + memory.media.

Fixtures generate fresh PNG/JPEG/WEBP files under tmp_path each run so
no binary assets are committed to git.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from memory import media
from memory.chunker_image import chunk_image


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def png_file(tmp_path: Path) -> Path:
    from PIL import Image
    p = tmp_path / "x.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(p)
    return p


@pytest.fixture
def jpeg_file(tmp_path: Path) -> Path:
    from PIL import Image
    p = tmp_path / "y.jpg"
    Image.new("RGB", (80, 40), (0, 255, 0)).save(p, format="JPEG")
    return p


@pytest.fixture
def webp_file(tmp_path: Path) -> Path:
    from PIL import Image
    p = tmp_path / "z.webp"
    Image.new("RGB", (32, 32), (0, 0, 255)).save(p, format="WEBP")
    return p


@pytest.fixture
def huge_png(tmp_path: Path) -> Path:
    from PIL import Image
    # 4096x4096 noise would be too big; a solid-colour image still proves the
    # chunker happily downsizes past 20 KB without issue.
    p = tmp_path / "huge.png"
    Image.new("RGB", (4096, 4096), (128, 64, 200)).save(p)
    return p


# ---------------------------------------------------------------------------
# detect_mime
# ---------------------------------------------------------------------------


def test_detect_mime_png(png_file: Path):
    assert media.detect_mime(png_file) == "image/png"


def test_detect_mime_jpeg(jpeg_file: Path):
    assert media.detect_mime(jpeg_file) == "image/jpeg"


def test_detect_mime_webp(webp_file: Path):
    assert media.detect_mime(webp_file) == "image/webp"


def test_detect_mime_unknown(tmp_path: Path):
    p = tmp_path / "weird.xyz"
    p.write_bytes(b"nope")
    with pytest.raises(ValueError):
        media.detect_mime(p)


# ---------------------------------------------------------------------------
# make_image_thumbnail
# ---------------------------------------------------------------------------


def test_thumbnail_fits_budget(huge_png: Path):
    b64 = media.make_image_thumbnail(huge_png)
    # round-trip base64 → bytes must succeed
    raw = base64.b64decode(b64)
    assert raw[:2] == b"\xff\xd8", "expected JPEG magic"
    assert len(b64) < 20_000


# ---------------------------------------------------------------------------
# chunk_image
# ---------------------------------------------------------------------------


def test_chunk_image_happy_path(png_file: Path):
    chunk = chunk_image(png_file, caption="red square")
    assert chunk.kind == "image"
    assert chunk.media_type == "image/png"
    assert chunk.content == "red square"
    assert chunk.preview_b64 and len(chunk.preview_b64) > 0
    assert chunk.metadata["w"] == 64
    assert chunk.metadata["h"] == 64
    assert len(chunk.metadata["sha256"]) == 64
    assert chunk.metadata["bytes"] > 0
    assert chunk.path == png_file
    assert chunk.media_ref.endswith("x.png")


def test_chunk_image_falls_back_to_stem(png_file: Path):
    chunk = chunk_image(png_file)
    assert chunk.content == "x"


def test_chunk_image_unknown_format(tmp_path: Path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ValueError):
        chunk_image(p)
