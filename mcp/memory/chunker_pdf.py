"""PDF → one MediaChunk per page.

For v1 we emit one chunk per page (instead of batching ≤ 6 pages per
embedding request): per-page chunks are easier to debug, audit, and
surface in the viewer. Batching can be reintroduced later as an
embedder-level optimisation without changing this API.

Rendering uses pypdfium2 at 150 DPI; each page is saved to a temp PNG
that the caller must keep alive for the duration of the embedding call
(pass an ExitStack / TemporaryDirectory and close it only after the
store has persisted the rows).
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import ExitStack
from pathlib import Path

from . import media
from .types import MediaChunk

logger = logging.getLogger("memory")


# 150 DPI -> scale = 150/72 ≈ 2.083 in pypdfium2 terms.
_RENDER_SCALE = 150.0 / 72.0
_PREVIEW_TEXT_CHARS = 500


def chunk_pdf(path: Path, stack: ExitStack | None = None) -> list[MediaChunk]:
    """Split a PDF into one MediaChunk per page.

    Args:
        path: Absolute path to a `.pdf` file.
        stack: Optional ExitStack owned by the caller. The temporary
            directory holding the per-page PNG renders is registered on
            this stack so the PNGs survive until the caller closes it
            (typically after the embedder has consumed them). When
            None, a module-local stack is used and the temp dir is
            cleaned up at next GC — fine for tests that embed
            synchronously.

    Returns:
        List of MediaChunk, one per page, with:
          - kind='pdf_page'
          - media_ref=f'{abs_path}#page={n}'   (1-indexed)
          - content = first 500 chars of extracted text, or filename stem
          - preview_b64 = 256×256 JPEG thumbnail of the page
          - path = Path to the rendered PNG (embedder input)

    Raises:
        FileNotFoundError: path missing / not a regular file.
        ValueError: non-pdf suffix.
        RuntimeError: pypdfium2 fails to open (corrupt PDF, etc).
    """
    if not path.is_file():
        raise FileNotFoundError(f"chunk_pdf: not a file: {path}")

    mime = media.detect_mime(path)
    if mime != "application/pdf":
        raise ValueError(f"chunk_pdf: {path} has MIME {mime!r}, expected application/pdf")

    try:
        import pypdfium2 as pdfium  # lazy; heavy native dep
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "chunk_pdf: pypdfium2 is not installed. "
            "Add `pypdfium2>=4.30` to mcp/requirements.txt."
        ) from e

    owned_stack = stack is None
    if stack is None:
        stack = ExitStack()
    tmpdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="aleph-pdf-")))

    abs_path = path.resolve()
    sha256 = media.sha256_file(path)
    stem = path.stem

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as e:
        if owned_stack:
            stack.close()
        raise RuntimeError(f"chunk_pdf: pypdfium2 failed to open {path}: {e}") from e

    chunks: list[MediaChunk] = []
    try:
        total = len(pdf)
        for i in range(total):
            page = pdf[i]
            # Render page to PNG at 150 DPI.
            try:
                bitmap = page.render(scale=_RENDER_SCALE)
                pil_image = bitmap.to_pil()
            finally:
                page.close()

            png_path = tmpdir / f"page-{i + 1:04d}.png"
            pil_image.save(png_path, format="PNG", optimize=True)

            # Thumbnail.
            preview = media.make_image_thumbnail(png_path)

            # Text extraction (best-effort; empty for image-only PDFs).
            text = ""
            try:
                # Re-open a fresh page for text extraction — pypdfium2 closes
                # its text page automatically with the page handle.
                tp = pdf[i]
                try:
                    text_page = tp.get_textpage()
                    try:
                        text = text_page.get_text_range() or ""
                    finally:
                        text_page.close()
                finally:
                    tp.close()
            except Exception as e:  # pragma: no cover
                logger.debug("chunk_pdf: text extraction failed for %s p%d: %s",
                             path.name, i + 1, e)

            text = (text or "").strip()
            snippet = text[:_PREVIEW_TEXT_CHARS] if text else stem

            chunks.append(MediaChunk(
                kind="pdf_page",
                content=snippet,
                media_ref=f"{abs_path}#page={i + 1}",
                media_type="application/pdf",
                preview_b64=preview,
                metadata={
                    "sha256_src": sha256,
                    "page": i + 1,
                    "total_pages": total,
                    "text_chars": len(text),
                },
                path=png_path,
            ))
    finally:
        pdf.close()
        if owned_stack:
            # Caller didn't provide a stack: keep tmpdir alive for the life
            # of the returned paths by NOT closing owned_stack here. The
            # TemporaryDirectory will be cleaned when the ExitStack is GC'd.
            # For deterministic cleanup, callers should pass their own stack.
            pass

    logger.info("chunk_pdf: %s -> %d page chunks", path.name, len(chunks))
    return chunks


__all__ = ["chunk_pdf"]
