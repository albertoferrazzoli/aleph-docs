"""Tests for memory.chunker_pdf.

Generates a 3-page PDF in a temp file using pypdfium2's low-level helpers
(via the `pypdf` companion library is avoided — we stick to pypdfium2's
native writer so the test deps stay aligned with prod). If that path
isn't available, we fall back to building the PDF with Pillow's
multi-frame save, which pypdfium2 can still read.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pytest

from memory.chunker_pdf import chunk_pdf


def _make_3_page_pdf(dest: Path) -> None:
    """Produce a 3-page PDF at `dest` with one letter/number per page.

    Strategy: use Pillow to build three RGB images and write them as a
    single multi-page PDF. Pillow's PDF writer is part of the `Pillow`
    dep already listed in mcp/requirements.txt, and pypdfium2 reads it
    fine.
    """
    from PIL import Image, ImageDraw, ImageFont  # lazy

    pages: list[Image.Image] = []
    for label in ("1", "2", "3"):
        im = Image.new("RGB", (400, 560), color=(255, 255, 255))
        d = ImageDraw.Draw(im)
        try:
            font = ImageFont.load_default()
        except Exception:  # pragma: no cover
            font = None
        d.text((180, 260), f"Page {label}", fill=(0, 0, 0), font=font)
        pages.append(im)

    first, rest = pages[0], pages[1:]
    first.save(dest, format="PDF", save_all=True, append_images=rest)


def test_chunk_pdf_emits_one_chunk_per_page(tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    _make_3_page_pdf(pdf_path)
    assert pdf_path.exists()

    stack = ExitStack()
    chunks = chunk_pdf(pdf_path, stack=stack)
    assert len(chunks) == 3

    for i, c in enumerate(chunks, start=1):
        assert c.kind == "pdf_page"
        assert c.media_type == "application/pdf"
        assert c.media_ref.endswith(f"#page={i}")
        # media_ref's path portion is the absolute source path.
        assert c.media_ref.split("#", 1)[0] == str(pdf_path.resolve())
        assert c.preview_b64, f"page {i} has empty preview"
        assert len(c.preview_b64) > 100
        assert c.metadata["page"] == i
        assert c.metadata["total_pages"] == 3
        assert "sha256_src" in c.metadata
        # The rendered PNG path must exist while the caller's stack is alive.
        assert c.path is not None
        assert c.path.exists()
    stack.close()
    # After close, tmpdir is gone.
    for c in chunks:
        assert not c.path.exists()


def test_chunk_pdf_rejects_non_pdf(tmp_path: Path):
    fake = tmp_path / "not-a-pdf.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    with pytest.raises(ValueError):
        # suffix is .png — detect_mime returns image/png, chunk_pdf rejects.
        chunk_pdf(fake)


def test_chunk_pdf_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        chunk_pdf(tmp_path / "nope.pdf")
