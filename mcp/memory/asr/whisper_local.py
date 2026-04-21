"""Local Whisper ASR backend — host HTTP bridge + in-container fallback.

Two tiers of local execution, tried in order:

1. **Host HTTP bridge** (preferred): `whisper.cpp` compiled with
   `-DWHISPER_METAL=1` running as an HTTP server on the host. Docker
   reaches it via `host.docker.internal` on macOS/Windows, via the
   default docker0 IP on Linux. Apple-Silicon Metal gives 5-10×
   realtime on large-v3 — the practical choice for bulk ingest.

2. **In-container fallback** (slow): `faster-whisper` on CPU inside
   the mcp container. Kicks in only when `ASR_HOST` is unset or
   unreachable. A one-time warning is logged so operators know the
   ingest will run at ~0.5× realtime.

Both tiers expose the same `transcribe(path, language)` surface — the
caller doesn't know which actually answered.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from .base import ASRBackend, ASRBackendError

log = logging.getLogger("memory.asr.whisper_local")


def _host() -> str:
    return (os.environ.get("ASR_HOST", "") or "").rstrip("/")


def _timeout_s() -> float:
    try:
        return float(os.environ.get("ASR_TIMEOUT_S", "600"))
    except ValueError:
        return 600.0


def _model() -> str:
    return os.environ.get("ASR_MODEL", "large-v3")


# Cached faster-whisper model — constructing it allocates the weights
# in memory (large-v3 ~3 GB) so we never want to rebuild per call.
_local_model = None
_local_model_name: Optional[str] = None
_local_warned = False


def _load_local_model():
    global _local_model, _local_model_name
    model_name = _model()
    if _local_model is not None and _local_model_name == model_name:
        return _local_model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover
        raise ASRBackendError(
            "whisper_local: neither ASR_HOST is set nor faster-whisper is "
            "installed. Either start whisper.cpp on the host and set "
            "ASR_HOST=http://host.docker.internal:8090 in .env, or "
            "`pip install faster-whisper>=1.0` inside the mcp container."
        ) from e
    # `int8` quantisation keeps the memory footprint manageable on CPU.
    # Device auto-picks CUDA/Metal when available; inside a Linux
    # container it falls back to CPU.
    _local_model = WhisperModel(
        model_name, device="auto", compute_type="int8",
    )
    _local_model_name = model_name
    log.info(
        "[asr] faster-whisper model=%s loaded (in-container fallback)",
        model_name,
    )
    return _local_model


async def _transcribe_via_host(audio_path: Path, language: str | None) -> str:
    url = f"{_host()}/inference"
    data: dict = {}
    if language:
        data["language"] = language
    # whisper.cpp server returns {"text": "..."} by default.
    async with httpx.AsyncClient(timeout=_timeout_s()) as client:
        with audio_path.open("rb") as fh:
            files = {"file": (audio_path.name, fh, "application/octet-stream")}
            try:
                r = await client.post(url, files=files, data=data)
            except httpx.ConnectError as e:
                raise ASRBackendError(
                    f"whisper_local: cannot reach ASR_HOST {_host()}: {e}"
                ) from e
    if r.status_code != 200:
        raise ASRBackendError(
            f"whisper_local: {_host()} returned HTTP {r.status_code}: "
            f"{r.text[:200]}"
        )
    try:
        payload = r.json()
    except Exception as e:
        raise ASRBackendError(
            f"whisper_local: non-JSON response from {_host()}: {e}"
        ) from e
    # whisper.cpp server shape: {"text": "..."} (or a list of segments
    # depending on build flags). Cover both.
    if isinstance(payload, dict):
        if "text" in payload:
            return (payload["text"] or "").strip()
        if "segments" in payload and isinstance(payload["segments"], list):
            return " ".join(
                (s.get("text") or "").strip()
                for s in payload["segments"]
                if isinstance(s, dict)
            ).strip()
    raise ASRBackendError(
        f"whisper_local: unexpected response shape from {_host()}: "
        f"{type(payload).__name__}"
    )


async def _transcribe_via_local(audio_path: Path, language: str | None) -> str:
    global _local_warned
    if not _local_warned:
        log.warning(
            "[asr] whisper_local falling back to in-container faster-whisper "
            "(CPU-bound, expect ~0.5× realtime). Set ASR_HOST to a Whisper "
            "HTTP server running on the host for Metal/GPU acceleration."
        )
        _local_warned = True

    import asyncio

    model = _load_local_model()

    def _run():
        segments, _info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=True,
        )
        # `segments` is a generator; join in-memory. Per-segment
        # timestamps are available but we don't use them at this layer.
        return " ".join((s.text or "").strip() for s in segments).strip()

    # faster-whisper is CPU-bound; run off the event loop.
    return await asyncio.to_thread(_run)


class WhisperLocalBackend:
    name = "whisper_local"
    price_usd_per_minute = 0.0

    async def transcribe(
        self, audio_path: Path, *, language: str | None = None,
    ) -> str:
        if not audio_path.is_file():
            raise ASRBackendError(
                f"whisper_local: file not found: {audio_path}"
            )
        if _host():
            return await _transcribe_via_host(audio_path, language)
        return await _transcribe_via_local(audio_path, language)


BACKEND = WhisperLocalBackend()

__all__ = ["BACKEND", "WhisperLocalBackend"]
