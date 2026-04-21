"""ASR backend registry + shared entry point.

Operators select the active backend with the `ASR_BACKEND` env var
(default `whisper_local`). The shared :func:`transcribe` wrapper
gates on `ASR_ENABLED` and never raises — any backend failure
becomes `""`, so the surrounding chunker can always fall back to
the legacy `"scene N @ Xs"` content.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .base import ASRBackend, ASRBackendError

log = logging.getLogger("memory.asr")


_BACKENDS = {
    "whisper_local": "memory.asr.whisper_local",
    "openai":        "memory.asr.openai",
    "gemini":        "memory.asr.gemini",
}


_singleton: Optional[ASRBackend] = None
_singleton_name: Optional[str] = None


def _resolve_backend_name() -> str:
    name = os.environ.get("ASR_BACKEND", "whisper_local").strip() or "whisper_local"
    if name not in _BACKENDS:
        raise ASRBackendError(
            f"unknown ASR_BACKEND={name!r}. "
            f"Valid options: {sorted(_BACKENDS.keys())}"
        )
    return name


def get_backend() -> ASRBackend:
    """Return the singleton for the currently selected backend.

    Re-imports the backend module on every change of `ASR_BACKEND` so
    tests that monkeypatch the env var can switch at runtime without
    a full process restart.
    """
    global _singleton, _singleton_name
    name = _resolve_backend_name()
    if _singleton is not None and _singleton_name == name:
        return _singleton

    import importlib

    mod = importlib.import_module(_BACKENDS[name])
    if not hasattr(mod, "BACKEND"):
        raise ASRBackendError(
            f"ASR backend {name!r} at {_BACKENDS[name]!r} does not expose "
            f"a module-level `BACKEND` singleton."
        )
    _singleton = mod.BACKEND
    _singleton_name = name
    log.info(
        "[asr] active backend=%s price=~$%.4f/min",
        _singleton.name, _singleton.price_usd_per_minute,
    )
    return _singleton


async def transcribe(
    audio_path: Path, *, language: str | None = None,
) -> str:
    """Transcribe an audio or video file using the active ASR backend.

    Never raises. Returns `""` when:
      - ASR is globally disabled via `ASR_ENABLED=false`
      - the backend raises for any reason (missing key, timeout,
        server unreachable, …)

    The swallowed failure is logged at WARNING level so operators
    can diagnose without losing the ingest pass.
    """
    if os.environ.get("ASR_ENABLED", "true").strip().lower() != "true":
        return ""
    try:
        backend = get_backend()
    except Exception as e:
        log.warning("[asr] backend resolution failed: %s", e)
        return ""
    try:
        return await backend.transcribe(audio_path, language=language)
    except ASRBackendError as e:
        log.warning(
            "[asr] %s failed on %s: %s", backend.name, audio_path, e,
        )
        return ""
    except Exception as e:
        log.warning(
            "[asr] %s crashed on %s: %s: %s",
            backend.name, audio_path, type(e).__name__, e,
        )
        return ""


__all__ = ["transcribe", "get_backend", "ASRBackend", "ASRBackendError"]
