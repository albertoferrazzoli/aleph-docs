"""Image → one memory row.

Single-file chunker for the `image` modality. No embedding happens here;
that is deferred to `store.upsert_media_chunk` so the store layer owns
backend selection, out_dim guarding, and audit.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import media
from .types import MediaChunk

logger = logging.getLogger("memory")


_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}


def chunk_image(path: Path, caption: str | None = None) -> MediaChunk:
    """Validate, thumbnail, and return a single MediaChunk.

    Args:
        path: Absolute path to the image file. Must be one of the
            allow-listed MIMEs (png/jpeg/webp).
        caption: Optional human caption — becomes `content` (and is what
            the audit trail / UI display). Falls back to the file stem.

    Raises:
        ValueError: unknown suffix or non-image MIME.
        FileNotFoundError: path does not exist / is not a regular file.
    """
    if not path.is_file():
        raise FileNotFoundError(f"chunk_image: not a file: {path}")

    mime = media.detect_mime(path)
    if mime not in _IMAGE_MIMES:
        raise ValueError(
            f"chunk_image: {path} has MIME {mime!r}, expected one of "
            f"{sorted(_IMAGE_MIMES)}"
        )

    # Thumbnail (≤ 20 KB base64).
    preview = media.make_image_thumbnail(path)

    # Dimensions via Pillow (cheap, already depend on it).
    from PIL import Image  # lazy

    with Image.open(path) as im:
        w, h = im.size

    stat = path.stat()
    meta = {
        "sha256": media.sha256_file(path),
        "w": int(w),
        "h": int(h),
        "bytes": int(stat.st_size),
    }

    content = (caption or path.stem).strip() or path.stem
    logger.debug(
        "chunk_image: %s mime=%s w=%d h=%d bytes=%d",
        path.name, mime, w, h, stat.st_size,
    )
    return MediaChunk(
        kind="image",
        content=content,
        media_ref=str(path.resolve()),
        media_type=mime,
        preview_b64=preview,
        metadata=meta,
        path=path,
    )


__all__ = ["chunk_image"]
