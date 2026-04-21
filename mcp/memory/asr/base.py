"""ASR backend protocol + shared errors.

Mirrors the `embedders/base.py` registry pattern: every concrete
backend implements a single async `transcribe(audio_path, language)`
coroutine, exposes a stable `name` for logs, and declares its
approximate per-minute cost for the README / operator output.

Backends must NOT raise on transient failures the caller can retry.
Unrecoverable problems (missing dependency, missing credential,
malformed config) should raise :class:`ASRBackendError` with a
human-readable message — the shared wrapper in `asr/__init__.py`
catches it and falls back to the legacy placeholder content.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


class ASRBackendError(RuntimeError):
    """Raised by a backend when it cannot produce a transcript.

    Swallowed by the module-level `transcribe()` wrapper, which
    returns `""` on any backend failure so the chunker stays lossless
    and the ingest never fails because of ASR.
    """


@runtime_checkable
class ASRBackend(Protocol):
    """The contract every ASR backend implements."""

    #: Short stable identifier used in logs, e.g. `"whisper_local"`.
    name: str

    #: Approximate per-minute cost in USD. `0.0` for local backends.
    #: Informational — surfaced in the cost README and in warning logs
    #: when a paid backend is activated so operators see it.
    price_usd_per_minute: float

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> str:
        """Return the transcript of a local audio/video file as a string.

        Args:
            audio_path: Absolute path to a file readable by ffmpeg.
                Must outlive the call; the backend may stream it.
            language: Optional ISO-639-1 hint (e.g. `"it"`, `"en"`).
                When `None` the backend auto-detects where supported.

        Returns:
            The plain-text transcript. An empty string is a legitimate
            return value (silent segment) — callers must handle it.

        Raises:
            ASRBackendError: unrecoverable (missing key, bad config).
        """
        ...


__all__ = ["ASRBackend", "ASRBackendError"]
