"""Bearer-token protection for the MCP HTTP transport.

The remote MCP endpoint (``/mcp``) is public HTTPS but only a single configured
identity may use it. This module provides the reusable transport guard that
enforces OAuth bearer-token authentication on the MCP path while keeping the
unauthenticated surface (``/healthz`` plus the OAuth discovery/metadata and flow
endpoints) reachable.

The guard is a Starlette/ASGI middleware (the ``mcp`` SDK transport is
Starlette-based). For every protected request it:

- extracts ``Authorization: Bearer <token>`` and validates it through
  :class:`~server.auth.provider.SingleUserOAuthProvider` (signature,
  audience, issuer, expiry, and allowlist re-check);
- on a missing/invalid/expired/wrong-audience/wrong-issuer/non-allowlisted
  token, returns HTTP ``401`` with a RFC 9728 / RFC 6750 ``WWW-Authenticate:
  Bearer`` challenge that points at the protected-resource metadata so clients
  can begin the OAuth flow;
- on success, stashes the decoded :class:`~server.auth.models.TokenClaims`
  on the request scope (``scope["mcp_token_claims"]``) and forwards the request.

Step 20 mounts the actual MCP ASGI app behind this middleware; here we only
build the reusable guard and the small helpers tests exercise.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from server.auth.metadata import PROTECTED_RESOURCE_METADATA_PATH
from server.auth.models import OAuthErrorCode, TokenClaims
from server.auth.provider import SingleUserOAuthProvider
from server.settings import McpSettings, get_mcp_settings

# Default path prefix the MCP transport is mounted at.
DEFAULT_MCP_PATH = "/mcp"

# Paths that must stay reachable without a bearer token: health and the OAuth
# discovery/metadata + flow endpoints clients hit *before* they hold a token.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/.well-known/",
    "/oauth/",
)

# Key under which validated claims are stored on the ASGI scope.
SCOPE_CLAIMS_KEY = "mcp_token_claims"


def resource_metadata_url(settings: McpSettings | None = None) -> str:
    """Return the absolute protected-resource metadata URL for the challenge."""
    settings = settings or get_mcp_settings()
    issuer = settings.issuer()
    return issuer.rstrip("/") + PROTECTED_RESOURCE_METADATA_PATH


def build_www_authenticate(
    *,
    resource_metadata: str,
    error: str | None = None,
    error_description: str | None = None,
) -> str:
    """Build a RFC 9728-style ``WWW-Authenticate: Bearer`` challenge value.

    Always advertises ``resource_metadata`` so a client can discover the OAuth
    authorization server. When the failure is a presented-but-invalid token,
    ``error``/``error_description`` are included per RFC 6750 section 3.
    """
    parts = [f'resource_metadata="{resource_metadata}"']
    if error:
        parts.append(f'error="{error}"')
    if error_description:
        # Keep the description quote-safe for a header value.
        safe = error_description.replace('"', "'")
        parts.append(f'error_description="{safe}"')
    return "Bearer " + ", ".join(parts)


def extract_bearer_token(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """Return the bearer token from raw ASGI headers, or ``None`` if absent.

    Accepts the ``Authorization: Bearer <token>`` scheme case-insensitively.
    """
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        try:
            decoded = value.decode("latin-1").strip()
        except UnicodeDecodeError:
            return None
        scheme, _, token = decoded.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        return None
    return None


def is_public_path(path: str) -> bool:
    """Return ``True`` for paths reachable without a bearer token."""
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _is_protected_path(path: str, mcp_path: str) -> bool:
    """Return ``True`` when ``path`` is under the protected MCP mount."""
    if is_public_path(path):
        return False
    return path == mcp_path or path.startswith(mcp_path.rstrip("/") + "/")


class BearerAuthMiddleware:
    """ASGI middleware enforcing OAuth bearer auth on the MCP transport.

    Wrap the MCP ASGI app with this middleware. Requests to the configured MCP
    path require a valid bearer token; ``/healthz`` and the OAuth
    metadata/flow endpoints pass through untouched. The middleware is
    constructed with an explicit provider/settings for testability and falls
    back to the cached process settings otherwise.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        provider: SingleUserOAuthProvider | None = None,
        settings: McpSettings | None = None,
        mcp_path: str = DEFAULT_MCP_PATH,
    ) -> None:
        self.app = app
        self._settings = settings
        self._provider = provider
        self.mcp_path = mcp_path

    @property
    def settings(self) -> McpSettings:
        """Return the bound settings, defaulting to the cached process settings."""
        return self._settings or get_mcp_settings()

    @property
    def provider(self) -> SingleUserOAuthProvider:
        """Return the bound provider, building one from settings on demand."""
        if self._provider is None:
            self._provider = SingleUserOAuthProvider.for_settings(self.settings)
        return self._provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self.settings.auth_enabled or not _is_protected_path(
            path, self.mcp_path
        ):
            await self.app(scope, receive, send)
            return

        token = extract_bearer_token(scope.get("headers", []))
        if token is None:
            await self._reject(
                send,
                error=None,
                description="A bearer token is required for the MCP endpoint.",
            )
            return

        claims = self.provider.verify_token_sync(token)
        if claims is None:
            await self._reject(
                send,
                error=OAuthErrorCode.INVALID_TOKEN,
                description="The bearer token is invalid or expired.",
            )
            return

        scope[SCOPE_CLAIMS_KEY] = claims
        await self.app(scope, receive, send)

    async def _reject(
        self, send: Send, *, error: str | None, description: str | None
    ) -> None:
        """Send a 401 response carrying the OAuth WWW-Authenticate challenge."""
        challenge = build_www_authenticate(
            resource_metadata=resource_metadata_url(self.settings),
            error=error,
            error_description=description,
        )
        payload = {"error": error or "unauthorized"}
        if description:
            payload["error_description"] = description
        body = json.dumps(payload).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"www-authenticate", challenge.encode("latin-1")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send(
            {"type": "http.response.start", "status": 401, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})


def require_bearer(
    provider: SingleUserOAuthProvider | None = None,
    *,
    settings: McpSettings | None = None,
    mcp_path: str = DEFAULT_MCP_PATH,
) -> Callable[[ASGIApp], BearerAuthMiddleware]:
    """Return a middleware factory binding a provider/settings to the guard.

    Usable with Starlette's ``Middleware(require_bearer(provider))`` pattern or
    by calling the returned factory directly on an ASGI app.
    """

    def factory(app: ASGIApp) -> BearerAuthMiddleware:
        return BearerAuthMiddleware(
            app, provider=provider, settings=settings, mcp_path=mcp_path
        )

    return factory


def get_token_claims(scope: Scope) -> TokenClaims | None:
    """Return validated claims stashed on the ASGI ``scope`` by the middleware."""
    claims = scope.get(SCOPE_CLAIMS_KEY)
    return claims if isinstance(claims, TokenClaims) else None


__all__ = [
    "BearerAuthMiddleware",
    "DEFAULT_MCP_PATH",
    "SCOPE_CLAIMS_KEY",
    "build_www_authenticate",
    "extract_bearer_token",
    "get_token_claims",
    "is_public_path",
    "require_bearer",
    "resource_metadata_url",
]
