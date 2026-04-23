"""Session auth unit tests.

Covers:
* bcrypt htpasswd parsing (both `$2a$/$2b$` and Apache's `$2y$`)
* session create / validate / sliding TTL / revoke
* /auth/login, /auth/me, /auth/logout round-trip via FastAPI TestClient
"""

from __future__ import annotations

import os
import tempfile

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def htpasswd_file():
    path = tempfile.mktemp()
    h = bcrypt.hashpw(b"s3cret", bcrypt.gensalt(rounds=4)).decode()
    with open(path, "w") as f:
        f.write(f"alberto:{h}\n")
    yield path
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def session_env(htpasswd_file, monkeypatch):
    monkeypatch.setenv("ALEPH_HTPASSWD_FILE", htpasswd_file)
    monkeypatch.setenv("ALEPH_SESSIONS_DB", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("ALEPH_SESSION_TTL_HOURS", "24")
    monkeypatch.setenv("ALEPH_COOKIE_SECURE", "0")
    # TestClient serves the router at "/", so the default cookie path
    # "/aleph" would prevent the browser from echoing the cookie back.
    monkeypatch.setenv("ALEPH_COOKIE_PATH", "/")
    from backend.sessions import reset_for_tests
    reset_for_tests()
    yield


def test_htpasswd_bcrypt(session_env):
    from backend.sessions import get_htpasswd
    h = get_htpasswd()
    assert h.verify("alberto", "s3cret") is True
    assert h.verify("alberto", "wrong") is False
    assert h.verify("nobody", "whatever") is False
    assert h.verify("", "s3cret") is False
    assert h.verify("alberto", "") is False


def test_htpasswd_2y_variant(tmp_path, monkeypatch):
    """Apache's `htpasswd -B` emits `$2y$` — bcrypt lib needs `$2b$`."""
    pw = "apache-pw"
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()
    h_2y = "$2y$" + h[4:]  # swap prefix
    p = tmp_path / "htpasswd"
    p.write_text(f"alice:{h_2y}\n")
    monkeypatch.setenv("ALEPH_HTPASSWD_FILE", str(p))
    from backend.sessions import reset_for_tests, get_htpasswd
    reset_for_tests()
    assert get_htpasswd().verify("alice", pw) is True


def test_htpasswd_rejects_legacy_hashes(tmp_path, monkeypatch):
    p = tmp_path / "htpasswd"
    p.write_text("legacy:$apr1$oldmd5$junk\n")  # apache MD5, not bcrypt
    monkeypatch.setenv("ALEPH_HTPASSWD_FILE", str(p))
    from backend.sessions import reset_for_tests, get_htpasswd
    reset_for_tests()
    assert get_htpasswd().verify("legacy", "anything") is False


def test_session_lifecycle(session_env):
    from backend.sessions import get_store
    store = get_store()
    sess = store.create("alberto")
    assert sess.username == "alberto"
    assert len(sess.token) > 20  # url-safe random

    validated = store.validate(sess.token)
    assert validated is not None
    assert validated.username == "alberto"

    assert store.validate("not-a-real-token") is None
    assert store.validate("") is None

    assert store.revoke(sess.token) is True
    assert store.validate(sess.token) is None
    assert store.revoke(sess.token) is False  # second revoke is a no-op


def test_login_me_logout_roundtrip(session_env, monkeypatch):
    """Full happy path via FastAPI TestClient."""
    from backend.auth_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    r = client.post("/auth/login", json={"username": "alberto", "password": "s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["username"] == "alberto"
    assert "token" in body
    # Cookie should be set in response.
    assert any(c for c in r.cookies if c == "aleph_session")

    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is True

    r = client.post("/auth/logout")
    assert r.status_code == 200

    # After logout, /me is 401.
    client.cookies.clear()
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_login_rejects_bad_password(session_env):
    from backend.auth_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/auth/login", json={"username": "alberto", "password": "nope"})
    assert r.status_code == 401


def test_bearer_token_fallback(session_env):
    from backend.auth_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/auth/login", json={"username": "alberto", "password": "s3cret"})
    token = r.json()["token"]

    # Fresh client (no cookies) authenticates via Bearer header.
    fresh = TestClient(app)
    r = fresh.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == "alberto"


def test_query_token_fallback(session_env):
    from backend.auth_routes import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    r = client.post("/auth/login", json={"username": "alberto", "password": "s3cret"})
    token = r.json()["token"]

    fresh = TestClient(app)
    r = fresh.get(f"/auth/me?token={token}")
    assert r.status_code == 200
