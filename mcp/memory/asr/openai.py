"""OpenAI ASR backend — `whisper-1` via the audio transcriptions API.

Use when you want a fully managed ASR without installing anything
locally. `whisper-1` is $0.006/min at the time of writing — cheaper
than running a GPU and typically more robust than CPU `faster-whisper`
for short segments.

Activated with `ASR_BACKEND=openai`. Requires `OPENAI_API_KEY`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .base import ASRBackend, ASRBackendError

log = logging.getLogger("memory.asr.openai")


def _model() -> str:
    return os.environ.get("ASR_OPENAI_MODEL", "whisper-1")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("ASR_TIMEOUT_S", "600"))
    except ValueError:
        return 600.0


class OpenAIBackend:
    name = "openai"
    # $0.006 / minute for whisper-1 as of late 2025.
    price_usd_per_minute = 0.006

    def _client(self):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ASRBackendError(
                "openai ASR backend requires OPENAI_API_KEY in the env"
            )
        try:
            # Lazy import so a project that never uses this backend
            # doesn't pay the `openai` SDK import cost at startup.
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise ASRBackendError(
                "openai package not installed; add openai>=1.0 to "
                "mcp/requirements.txt"
            ) from e
        return OpenAI(api_key=api_key, timeout=_timeout_s())

    async def transcribe(
        self, audio_path: Path, *, language: str | None = None,
    ) -> str:
        if not audio_path.is_file():
            raise ASRBackendError(f"openai: file not found: {audio_path}")
        client = self._client()
        model = _model()

        def _run() -> str:
            with audio_path.open("rb") as fh:
                kwargs: dict = {
                    "model": model,
                    "file": fh,
                    "response_format": "text",
                }
                if language:
                    kwargs["language"] = language
                # `response_format="text"` returns a str, not a JSON envelope.
                resp = client.audio.transcriptions.create(**kwargs)
            if isinstance(resp, str):
                return resp.strip()
            # In case a future SDK change returns an object, extract safely.
            text = getattr(resp, "text", None)
            if text is None:
                raise ASRBackendError(
                    f"openai: unexpected response type {type(resp).__name__}"
                )
            return str(text).strip()

        try:
            return await asyncio.to_thread(_run)
        except ASRBackendError:
            raise
        except Exception as e:
            raise ASRBackendError(f"openai: {type(e).__name__}: {e}") from e


BACKEND = OpenAIBackend()

__all__ = ["BACKEND", "OpenAIBackend"]
