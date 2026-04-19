"""Bearer token authentication middleware for the Aleph Docs MCP server."""

import hmac
import os
from urllib.parse import parse_qs


_BLOCKED_PATTERNS = (".env", ".sqlite", ".db", ".key", ".pem")


class APIKeyMiddleware:
    """ASGI middleware validating Bearer token against MCP_API_KEY.

    Token is accepted via Authorization header or ?token= query param
    (the latter is kept for Claude.ai custom connector compatibility).
    Comparison is constant-time to avoid timing attacks.
    """

    def __init__(self, app):
        self.app = app
        self.api_key = os.environ.get("MCP_API_KEY", "")

    def _extract_token(self, scope):
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            qs = parse_qs(scope.get("query_string", b"").decode())
            token = qs.get("token", [""])[0]
        return token

    def _valid(self, token: str) -> bool:
        if not self.api_key or not token:
            return False
        return hmac.compare_digest(token.encode(), self.api_key.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if any(p in path.lower() for p in _BLOCKED_PATTERNS):
                return await self._send_error(send, 403, "Forbidden")

            if not self._valid(self._extract_token(scope)):
                return await self._send_error(send, 401, "Unauthorized")

        await self.app(scope, receive, send)

    async def _send_error(self, send, status, msg):
        body = f'{{"error": "{msg}"}}'.encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": body})
