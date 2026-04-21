"""Gemini ASR backend — audio transcription via `generate_content`.

Gemini doesn't expose a dedicated "transcriptions" endpoint; we upload
the audio via the Files API, call `generate_content` on a Flash model
with a minimal "transcribe verbatim" instruction, and strip the
response. For a trading course (clean mic, single speaker) Flash is
plenty — if accuracy disappoints, bump `ASR_GEMINI_MODEL` to
`gemini-2.5-pro`.

Activated with `ASR_BACKEND=gemini`. Reuses the `GOOGLE_API_KEY`
already configured for the embedder, so no new credential.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path

from .base import ASRBackend, ASRBackendError

log = logging.getLogger("memory.asr.gemini")


# Keep the prompt short and directive — models otherwise insert
# meta-commentary ("Here is the transcription:") that contaminates
# the text embedding downstream.
_PROMPT = (
    "Transcribe this audio verbatim. "
    "Output the transcript only — no commentary, no timestamps, "
    "no speaker labels, no formatting, no quotation marks. "
    "If the audio is silent or unintelligible, return an empty string."
)


def _model() -> str:
    return os.environ.get("ASR_GEMINI_MODEL", "gemini-2.5-flash")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("ASR_TIMEOUT_S", "600"))
    except ValueError:
        return 600.0


def _detect_mime(path: Path) -> str:
    # mimetypes is bare; handle the common ones explicitly so that
    # container uploads always succeed.
    suffix = path.suffix.lower()
    if suffix in (".mp3",):
        return "audio/mpeg"
    if suffix in (".wav",):
        return "audio/wav"
    if suffix in (".mp4", ".m4a"):
        return "audio/mp4"
    if suffix in (".mov",):
        return "video/quicktime"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class GeminiBackend:
    name = "gemini"
    # Rough order-of-magnitude: gemini-2.5-flash audio input is
    # ~$0.30/M input tokens × ~50 tokens/s → ~$0.01/minute.
    price_usd_per_minute = 0.01

    def _client(self):
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise ASRBackendError(
                "gemini ASR backend requires GOOGLE_API_KEY in the env "
                "(same key used for gemini-* embedders)"
            )
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover
            raise ASRBackendError(
                "google-genai package missing; check mcp/requirements.txt"
            ) from e
        return genai.Client(api_key=api_key)

    async def transcribe(
        self, audio_path: Path, *, language: str | None = None,
    ) -> str:
        if not audio_path.is_file():
            raise ASRBackendError(f"gemini: file not found: {audio_path}")
        client = self._client()
        model = _model()
        mime = _detect_mime(audio_path)

        prompt = _PROMPT
        if language:
            prompt += f" The audio is in {language}."

        def _run() -> str:
            uploaded = None
            try:
                uploaded = client.files.upload(
                    file=str(audio_path),
                    config={"mime_type": mime},
                )
                resp = client.models.generate_content(
                    model=model,
                    contents=[prompt, uploaded],
                )
            finally:
                # Uploaded files persist on Google's side until TTL — ~2
                # days by default. Best-effort cleanup so the account
                # doesn't accumulate orphans during bulk ingest.
                if uploaded is not None:
                    try:
                        client.files.delete(name=uploaded.name)
                    except Exception as e:
                        log.debug(
                            "[asr.gemini] file delete failed for %s: %s",
                            getattr(uploaded, "name", "?"), e,
                        )
            text = getattr(resp, "text", None) or ""
            return text.strip()

        try:
            return await asyncio.to_thread(_run)
        except ASRBackendError:
            raise
        except Exception as e:
            raise ASRBackendError(f"gemini: {type(e).__name__}: {e}") from e


BACKEND = GeminiBackend()

__all__ = ["BACKEND", "GeminiBackend"]
