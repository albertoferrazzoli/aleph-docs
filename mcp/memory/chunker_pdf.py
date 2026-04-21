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
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from . import media
from .embedders import get_backend
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

    # If the caller provided a stack, tmpdir lifetime is scoped to it.
    # Otherwise use mkdtemp (survives until OS /tmp reaper) so the PNG
    # page files stay available for the subsequent embedding call that
    # happens after this function returns.
    owned_stack = stack is None
    if owned_stack:
        tmpdir = Path(tempfile.mkdtemp(prefix="aleph-pdf-"))
    else:
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

    # Zero-cost text-only mode: skip page rendering + embedded-image
    # extraction entirely, emit one pdf_text chunk per page with the
    # extracted text as content. Embedding is done as text by the
    # store, so works with any backend including local Ollama.
    hybrid = os.environ.get("HYBRID_MEDIA_EMBEDDING", "true").strip().lower() == "true"
    # Route rendered pages through whichever modality the active backend
    # supports. gemini-* declare "pdf", nomic_multimodal_local declares
    # "image" (same PNG bytes, different embedding surface), local has
    # neither and gets forced onto the text-only branch below.
    try:
        _modalities = get_backend().modalities
    except Exception:
        _modalities = frozenset()
    if "pdf" in _modalities:
        _page_kind = "pdf_page"
    elif "image" in _modalities:
        _page_kind = "image"
    else:
        # Backend can embed neither pdf nor image — force text-only mode
        # regardless of HYBRID_MEDIA_EMBEDDING so ingest does not fail.
        hybrid = False

    chunks: list[MediaChunk] = []
    try:
        total = len(pdf)
        for i in range(total):
            # ---- Text extraction (needed in both modes) ----
            text = ""
            try:
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

            # ---- Text-only branch (HYBRID_MEDIA_EMBEDDING=false) ----
            # Emit one pdf_text chunk per page; skip rendering and
            # embedded-image extraction entirely. Stops here for this page.
            if not hybrid:
                if not media.is_meaningful_text(text):
                    # Empty/junk page (e.g. a scanned PDF with no OCR
                    # layer, or a page with only a page number). Skip —
                    # we have nothing to embed as text, and the user
                    # opted out of image embedding.
                    continue
                chunks.append(MediaChunk(
                    kind="pdf_text",
                    content=text,
                    media_ref=f"{abs_path}#page={i + 1}",
                    media_type="text/plain",
                    preview_b64=None,
                    metadata={
                        "sha256_src": sha256,
                        "page": i + 1,
                        "total_pages": total,
                        "text_chars": len(text),
                    },
                    path=None,
                ))
                continue

            # ---- Hybrid branch (default): render + image embed ----
            page = pdf[i]
            try:
                bitmap = page.render(scale=_RENDER_SCALE)
                pil_image = bitmap.to_pil()
            finally:
                page.close()

            png_path = tmpdir / f"page-{i + 1:04d}.png"
            pil_image.save(png_path, format="PNG", optimize=True)

            # Thumbnail.
            preview = media.make_image_thumbnail(png_path)

            snippet = text[:_PREVIEW_TEXT_CHARS] if text else stem

            _page_meta = {
                "sha256_src": sha256,
                "page": i + 1,
                "total_pages": total,
                "text_chars": len(text),
                # Stash so callers can clean up after the embed step
                # (see tools/memory.py _route_media pattern).
                "_tmpdir": str(tmpdir) if owned_stack else None,
            }
            if _page_kind == "image":
                _page_meta["origin"] = "pdf_page"
            chunks.append(MediaChunk(
                kind=_page_kind,
                content=snippet,
                media_ref=f"{abs_path}#page={i + 1}",
                media_type=(
                    "application/pdf" if _page_kind == "pdf_page"
                    else "image/png"
                ),
                preview_b64=preview,
                metadata=_page_meta,
                path=png_path,
            ))
            # Also emit a paired pdf_text row when the page has
            # meaningful extracted text. Cheap (text embed) and makes
            # PDFs fully queryable by prose even with image-only
            # backends like nomic.
            if media.is_meaningful_text(text):
                chunks.append(MediaChunk(
                    kind="pdf_text",
                    content=text,
                    media_ref=f"{abs_path}#page={i + 1}",
                    media_type="text/plain",
                    preview_b64=preview,
                    metadata={
                        "sha256_src": sha256,
                        "page": i + 1,
                        "total_pages": total,
                        "text_chars": len(text),
                    },
                    path=None,
                ))

            # Additionally: extract embedded raster images as separate
            # `image` chunks so they are searchable at fine granularity.
            # IMPORTANT: PdfObjects returned by get_objects() hold refs
            # into the parent page. We must extract bitmaps AND save them
            # BEFORE closing the page, or pypdfium2 segfaults on
            # use-after-free.
            extracted: list[tuple[int, "Path", int, int]] = []  # (j, path, w, h)
            try:
                page_for_imgs = pdf[i]
                try:
                    img_objs = list(page_for_imgs.get_objects(
                        filter=(pdfium.raw.FPDF_PAGEOBJ_IMAGE,),
                        max_depth=5,
                    ))
                    for j, obj in enumerate(img_objs):
                        try:
                            bm = obj.get_bitmap()
                            pil = bm.to_pil()
                        except Exception as e:
                            logger.debug(
                                "chunk_pdf: bitmap failed p%d img%d: %s",
                                i + 1, j + 1, e,
                            )
                            continue
                        if pil.width < 64 or pil.height < 64:
                            # Skip tiny glyphs / icons — noise.
                            continue
                        img_path = tmpdir / f"page-{i + 1:04d}-img-{j + 1:03d}.png"
                        try:
                            pil.save(img_path, format="PNG", optimize=True)
                        except Exception as e:
                            logger.debug(
                                "chunk_pdf: save failed p%d img%d: %s",
                                i + 1, j + 1, e,
                            )
                            continue
                        extracted.append((j, img_path, pil.width, pil.height))
                finally:
                    page_for_imgs.close()
            except Exception as e:
                logger.debug("chunk_pdf: image pass failed p%d: %s", i + 1, e)

            # Now that the page is closed, the saved PNGs are independent
            # on-disk files — safe to thumbnail + wrap in MediaChunk.
            for j, img_path, w, h in extracted:
                try:
                    img_preview = media.make_image_thumbnail(img_path)
                except Exception as e:
                    logger.debug("chunk_pdf: thumbnail failed p%d img%d: %s",
                                 i + 1, j + 1, e)
                    continue
                chunks.append(MediaChunk(
                    kind="image",
                    content=f"Image {j + 1} of page {i + 1} · {stem}",
                    media_ref=f"{abs_path}#page={i + 1}&img={j + 1}",
                    media_type="image/png",
                    preview_b64=img_preview,
                    metadata={
                        "source_pdf": str(abs_path),
                        "source_pdf_sha256": sha256,
                        "page": i + 1,
                        "img_index": j + 1,
                        "w": w,
                        "h": h,
                        "_tmpdir": str(tmpdir) if owned_stack else None,
                    },
                    path=img_path,
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
