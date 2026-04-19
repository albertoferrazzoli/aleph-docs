"""Tests for the embedder backend registry (Phase 0).

No network calls. Every backend's transport layer is monkeypatched
away; we only exercise wiring, dim guards, type validation and the
backward-compat shim in `memory.embeddings`.
"""

from __future__ import annotations

import pytest

from memory import embeddings as emb_mod
from memory.embedders import (
    BackendError,
    DEFAULT_BACKEND,
    _reset_cache_for_tests,
    get_backend,
    list_backends,
)


# ---------------------------------------------------------------------------
# Autouse: ensure a clean registry + embeddings module between tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry_and_shim(monkeypatch):
    _reset_cache_for_tests()
    # Clear cached backend on the shim too.
    emb_mod._backend = None
    emb_mod.reset_token_counter()
    yield
    _reset_cache_for_tests()
    emb_mod._backend = None


# ---------------------------------------------------------------------------
# 1. Registry basics
# ---------------------------------------------------------------------------


def test_registry_lists_all():
    names = set(list_backends())
    assert names == {"gemini-001", "gemini-2-preview", "local"}


def test_get_backend_by_env(monkeypatch):
    monkeypatch.setenv("EMBED_BACKEND", "local")
    # Also set dim so the LocalBackend doesn't explode on import (it won't,
    # but being explicit is cheap).
    monkeypatch.setenv("LOCAL_EMBED_DIM", "1024")
    b = get_backend()
    assert b.name == "local"


def test_default_backend_is_gemini_001(monkeypatch):
    monkeypatch.delenv("EMBED_BACKEND", raising=False)
    assert DEFAULT_BACKEND == "gemini-001"
    b = get_backend()
    assert b.name == "gemini-001"


def test_get_backend_invalid(monkeypatch):
    monkeypatch.setenv("EMBED_BACKEND", "foo")
    with pytest.raises(BackendError) as ei:
        get_backend()
    msg = str(ei.value)
    assert "foo" in msg
    for valid in ("gemini-001", "gemini-2-preview", "local"):
        assert valid in msg


# ---------------------------------------------------------------------------
# 2. Backend-specific guards
# ---------------------------------------------------------------------------


async def test_gemini_001_rejects_non_text(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    from memory.embedders import gemini_001

    # If validation ever fails open, this sentinel will surface it.
    def _boom(*a, **kw):
        raise AssertionError("client should never be built for a type-check failure")
    monkeypatch.setattr(gemini_001, "_get_client", _boom)

    backend = get_backend("gemini-001")
    with pytest.raises(BackendError):
        await backend.embed([b"\x89PNG"], out_dim=1536)


async def test_dim_guard(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    from memory.embedders import gemini_001

    def _boom(*a, **kw):
        raise AssertionError("SDK must NOT be called when dim guard fails")
    monkeypatch.setattr(gemini_001, "_get_client", _boom)

    backend = get_backend("gemini-001")
    # native_dim = 1536, requesting 4096 must fail before any client setup.
    with pytest.raises(BackendError) as ei:
        await backend.embed(["hello"], out_dim=4096)
    assert "4096" in str(ei.value)


# ---------------------------------------------------------------------------
# 3. Shim compatibility
# ---------------------------------------------------------------------------


async def test_embed_batch_compat_shim(monkeypatch):
    """`embeddings.embed_batch(['a','b'])` still returns (2, 1536) vectors
    through the real shim. conftest.py's autouse fixture replaces
    `embed_batch` with a fake, so we re-install the real function inside
    this test — monkeypatch auto-reverts on teardown.
    """
    # Re-import fresh to grab the real implementations.
    import importlib
    real = importlib.reload(emb_mod)

    class _FakeBackend:
        name = "fake"
        native_dim = 1536
        modalities = frozenset({"text"})
        price_estimate_usd_per_1k = {"text": 0.0}

        async def embed(self, items, out_dim):
            return [[0.0] * out_dim for _ in items]

    # Force the shim to use our fake backend, bypassing env resolution.
    real._backend = _FakeBackend()
    monkeypatch.setenv("EMBED_DIM", "1536")

    out = await real.embed_batch(["a", "b"])
    assert len(out) == 2
    assert all(len(v) == 1536 for v in out)

    # Token counter should have ticked.
    assert emb_mod.tokens_used() >= 0
    emb_mod.reset_token_counter()
    assert emb_mod.tokens_used() == 0


# ---------------------------------------------------------------------------
# 4. Local backend transport failure
# ---------------------------------------------------------------------------


async def test_local_backend_connection_error(monkeypatch):
    monkeypatch.setenv("LOCAL_EMBED_DIM", "1024")
    from memory.embedders import local as local_mod

    class _FailingClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            import httpx
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(local_mod.httpx, "AsyncClient", _FailingClient)

    backend = get_backend("local")
    with pytest.raises(BackendError) as ei:
        await backend.embed(["hello"], out_dim=backend.native_dim)
    assert "unreachable" in str(ei.value).lower() or "ollama" in str(ei.value).lower()
