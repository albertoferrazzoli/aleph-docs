"""Shared media helpers: MIME detection, thumbnail generation, hashing.

Used by all media chunkers (image/video/audio/pdf). Pure functions; no
state. Pillow is the only hard dep added in this file; ffmpeg is invoked
via subprocess by the video/audio chunkers separately.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from pathlib import Path

logger = logging.getLogger("memory")


# Strict allowlist — suffix → MIME. We intentionally do NOT fall back to
# mimetypes.guess_type() here; the registry is the single source of truth
# for which modalities the system accepts.
_SUFFIX_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
}


def detect_mime(path: Path) -> str:
    """Return the MIME string for `path` based on its suffix (lowercased).

    Raises ValueError if the suffix is not in the allowlist — callers
    should catch this and surface a structured error to the tool layer.
    """
    suffix = path.suffix.lower()
    mime = _SUFFIX_TO_MIME.get(suffix)
    if mime is None:
        raise ValueError(
            f"media.detect_mime: unsupported suffix {suffix!r} for {path}. "
            f"Allowed: {sorted(_SUFFIX_TO_MIME.keys())}"
        )
    return mime


def sha256_file(path: Path, _chunk: int = 1 << 20) -> str:
    """Stream-hash a file. Safe for any size (reads 1 MiB at a time)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(_chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def make_image_thumbnail(
    path: Path,
    size: int = 256,
    max_bytes: int = 20_000,
) -> str:
    """Generate a center-cropped, base64-encoded JPEG thumbnail.

    Strategy:
        1. Open with Pillow (`PIL.Image.open`).
        2. Resize preserving aspect ratio, then center-crop to size×size.
        3. Encode as JPEG at quality 80, then step down 70/60/50/40 until
           the base64 string is < max_bytes.
        4. Return the base64 string (no `data:` prefix).

    Raises ValueError if even q=40 can't fit under `max_bytes`. In
    practice a 256x256 JPEG comfortably fits at q=80; hitting this
    branch suggests either an extreme aspect ratio or an anomalous
    image (e.g. pathological noise) — callers should log and skip.
    """
    from PIL import Image  # lazy import; Pillow is only needed here

    with Image.open(path) as im:
        im = im.convert("RGB")
        # Resize so the shorter side matches `size`, preserving aspect.
        w, h = im.size
        if w == 0 or h == 0:
            raise ValueError(f"media.make_image_thumbnail: empty image {path}")
        scale = max(size / w, size / h)
        new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        im = im.resize((new_w, new_h), Image.LANCZOS)
        # Center-crop to size × size.
        left = (new_w - size) // 2
        top = (new_h - size) // 2
        im = im.crop((left, top, left + size, top + size))

        for quality in (80, 70, 60, 50, 40):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            if len(b64) < max_bytes:
                logger.debug(
                    "media.make_image_thumbnail: %s -> q=%d bytes_b64=%d",
                    path.name, quality, len(b64),
                )
                return b64

    raise ValueError(
        f"media.make_image_thumbnail: cannot fit {path} under {max_bytes} bytes "
        f"even at JPEG q=40"
    )


__all__ = ["detect_mime", "sha256_file", "make_image_thumbnail"]
