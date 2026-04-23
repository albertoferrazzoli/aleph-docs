"""FastAPI router for /auth/* endpoints.

Kept separate from `main.py` so it's easy to mount under a different
prefix in tests / custom deployments.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import SESSION_COOKIE_NAME, optional_session, require_session
from .sessions import Session, get_htpasswd, get_store

log = logging.getLogger("aleph")

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=1024)


def _cookie_secure() -> bool:
    """Honor `ALEPH_COOKIE_SECURE=0` for local HTTP development.

    Default is True — production deploys terminate TLS upstream and
    the cookie MUST carry the Secure flag to avoid downgrade attacks.
    """
    raw = os.getenv("ALEPH_COOKIE_SECURE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _cookie_path() -> str:
    return os.getenv("ALEPH_COOKIE_PATH", "/aleph") or "/aleph"


def _set_session_cookie(response: Response, sess: Session) -> None:
    max_age = max(1, sess.expires_at - int(__import__("time").time()))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sess.token,
        max_age=max_age,
        expires=max_age,
        path=_cookie_path(),
        secure=_cookie_secure(),
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=_cookie_path(),
        secure=_cookie_secure(),
        httponly=True,
        samesite="strict",
    )


@router.post("/login")
async def login(body: LoginBody) -> Response:
    htpasswd = get_htpasswd()
    if not htpasswd.verify(body.username, body.password):
        # Constant-ish timing: checkpw already takes ~100ms on bcrypt
        # cost 5, so no extra sleep needed to mitigate user-enumeration.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )
    sess = get_store().create(body.username)
    payload = {
        "ok": True,
        "username": sess.username,
        "token": sess.token,
        "expires_at": sess.expires_at,
    }
    resp = JSONResponse(payload)
    _set_session_cookie(resp, sess)
    log.info("[aleph.auth] login user=%s ttl=%ds",
             sess.username, sess.expires_at - sess.created_at)
    return resp


@router.post("/logout")
async def logout(sess: Session = Depends(require_session)) -> Response:
    get_store().revoke(sess.token)
    resp = JSONResponse({"ok": True})
    _clear_session_cookie(resp)
    log.info("[aleph.auth] logout user=%s", sess.username)
    return resp


@router.get("/me")
async def me(sess: Session | None = Depends(optional_session)) -> JSONResponse:
    if sess is None:
        return JSONResponse(
            {"authenticated": False}, status_code=status.HTTP_401_UNAUTHORIZED
        )
    return JSONResponse({
        "authenticated": True,
        "username": sess.username,
        "expires_at": sess.expires_at,
    })
