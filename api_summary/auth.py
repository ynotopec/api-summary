import asyncio
import hashlib
from contextvars import ContextVar

import httpx
import jwt
from starlette.responses import JSONResponse

from .config import Settings

principal: ContextVar[str] = ContextVar("summary_principal")


def current_owner() -> str:
    try:
        return principal.get()
    except LookupError:
        raise PermissionError("Authentication required") from None


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.keys = None

    async def initialize(self):
        s = self.settings
        if not s.auth_enabled:
            return
        jwks = s.oidc_jwks_url
        if not jwks:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.get(s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
                response.raise_for_status()
                discovery = response.json()
            if discovery.get("issuer") != s.oidc_issuer:
                raise ValueError("OIDC discovery issuer mismatch")
            jwks = discovery["jwks_uri"]
            from urllib.parse import urlsplit

            if urlsplit(jwks).scheme != "https" and s.app_env != "development":
                raise ValueError("JWKS must use HTTPS")
        self.keys = jwt.PyJWKClient(jwks, cache_jwk_set=True, lifespan=300, timeout=10)

    async def authenticate(self, token: str) -> str:
        s = self.settings
        if not s.auth_enabled:
            return "development-only"
        key = await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=s.oidc_algorithms,
            audience=s.oidc_audience,
            issuer=s.oidc_issuer,
            options={"require": ["exp", "iss", "sub", "aud"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError("Invalid subject")
        scopes = claims.get("scope", claims.get("scp", ""))
        granted = set(scopes.split() if isinstance(scopes, str) else scopes)
        if not set(s.oidc_scopes.split()).issubset(granted):
            raise PermissionError("Missing scope")
        return hashlib.sha256(f"{claims['iss']}\0{claims['sub']}".encode()).hexdigest()


class SecurityMiddleware:
    """Identical identity boundary for REST and stateless MCP HTTP requests."""

    def __init__(self, app, authenticator: Authenticator):
        self.app = app
        self.auth = authenticator

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {
            "/healthz",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin1")
        if self.auth.settings.auth_enabled and not authorization.startswith("Bearer "):
            return await self._reject(scope, receive, send, 401)
        try:
            owner = await self.auth.authenticate(authorization.removeprefix("Bearer "))
        except PermissionError:
            return await self._reject(scope, receive, send, 403)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            return await self._reject(scope, receive, send, 401)
        # Buffer only small control/block requests, capped before JSON parsing.
        limit = self.auth.settings.max_chunk_bytes * 4 // 3 + 65536
        try:
            declared = int(headers.get(b"content-length", b"0") or b"0")
        except ValueError:
            return await JSONResponse({"detail": "Invalid content length"}, 400)(scope, receive, send)
        if declared > limit:
            return await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        context_token = principal.set(owner)
        try:
            await self.app(scope, bounded_receive, send)
        finally:
            principal.reset(context_token)

    async def _reject(self, scope, receive, send, status):
        metadata = self.auth.settings.public_base_url.rstrip("/") + "/.well-known/oauth-protected-resource"
        challenge = f'Bearer resource_metadata="{metadata}"'
        return await JSONResponse(
            {"detail": "Not authorized"}, status, headers={"WWW-Authenticate": challenge}
        )(scope, receive, send)
