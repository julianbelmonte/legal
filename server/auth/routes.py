"""OAuth discovery and flow routes for the remote MCP server.

This module builds the unauthenticated OAuth surface that remote MCP clients
(e.g. Claude Cowork) hit *before* they hold a bearer token:

- the two well-known discovery documents (RFC 8414 / RFC 9728) served from
  :mod:`server.auth.metadata`;
- ``POST /oauth/register`` — minimal dynamic client registration (RFC 7591)
  that echoes back a client id (public PKCE clients carry no secret);
- ``GET /oauth/authorize`` — a minimal single-user login/consent form;
- ``POST /oauth/authorize`` — verifies the login secret and the allowlisted
  email, mints a short-lived authorization code, and redirects back to the
  client's ``redirect_uri`` with ``code`` (and echoed ``state``);
- ``POST /oauth/token`` — exchanges the authorization code (+ PKCE verifier)
  for a signed JWT access token.

The flow is implemented with :class:`~server.auth.provider.SingleUserOAuthProvider`,
so it inherits PKCE (``S256``), the email allowlist, the login-secret gate, and
the constant-time comparisons defined there. These routes are intentionally
reachable without a bearer token; :class:`~server.auth.transport.BearerAuthMiddleware`
whitelists the ``/.well-known/`` and ``/oauth/`` prefixes.

A single :class:`~server.auth.provider.SingleUserOAuthProvider` instance is
shared between these routes and the transport guard so issued authorization
codes are visible to the token endpoint.
"""

from __future__ import annotations

import html
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from server.auth.metadata import (
    AUTHORIZATION_PATH,
    AUTHORIZATION_SERVER_METADATA_PATH,
    PROTECTED_RESOURCE_METADATA_PATH,
    REGISTRATION_PATH,
    TOKEN_PATH,
    authorization_server_metadata,
    protected_resource_metadata,
)
from server.auth.models import DEFAULT_SCOPE
from server.auth.provider import OAuthProviderError, SingleUserOAuthProvider
from server.settings import McpSettings, get_mcp_settings

ProviderFactory = Callable[[], SingleUserOAuthProvider]


def _oauth_error(error: str, description: str | None, status: int) -> JSONResponse:
    """Return an RFC 6749 OAuth error JSON response."""
    payload: dict[str, object] = {"error": error}
    if description:
        payload["error_description"] = description
    return JSONResponse(payload, status_code=status)


def _login_form(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str,
    code_challenge: str,
    code_challenge_method: str,
    message: str | None = None,
) -> HTMLResponse:
    """Render the minimal single-user login/consent form."""
    hidden = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }
    hidden_inputs = "".join(
        f'<input type="hidden" name="{html.escape(name)}" '
        f'value="{html.escape(value)}">'
        for name, value in hidden.items()
        if value
    )
    note = (
        f'<p style="color:#b00">{html.escape(message)}</p>' if message else ""
    )
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Legal MCP authorization</title></head><body>"
        "<h1>Authorize MCP access</h1>"
        f"{note}"
        f'<form method="post" action="{html.escape(AUTHORIZATION_PATH)}">'
        f"{hidden_inputs}"
        '<p><label>Email <input type="email" name="email" required></label></p>'
        '<p><label>Login secret '
        '<input type="password" name="secret" required></label></p>'
        '<p><button type="submit">Authorize</button></p>'
        "</form></body></html>"
    )
    return HTMLResponse(body)


def _redirect_with_code(redirect_uri: str, code: str, state: str) -> RedirectResponse:
    """Redirect back to the client's ``redirect_uri`` carrying ``code``/``state``."""
    query: dict[str, str] = {"code": code}
    if state:
        query["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urlencode(query), status_code=302)


def build_oauth_routes(
    provider_factory: ProviderFactory | None = None,
    *,
    settings: McpSettings | None = None,
) -> list[Route]:
    """Build the OAuth discovery + flow routes wired to a shared provider.

    A single :class:`SingleUserOAuthProvider` is constructed once (lazily) and
    shared across the authorize/token endpoints so the in-memory authorization
    codes minted by ``authorize`` are redeemable by ``token``. ``settings`` (and
    the derived provider) default to the cached process settings, recomputed at
    request time where they affect discovery metadata.
    """

    def _settings() -> McpSettings:
        return settings or get_mcp_settings()

    if provider_factory is None:
        _cached: dict[str, SingleUserOAuthProvider] = {}

        def _provider() -> SingleUserOAuthProvider:
            if "p" not in _cached:
                _cached["p"] = SingleUserOAuthProvider.for_settings(_settings())
            return _cached["p"]
    else:
        _provider = provider_factory

    async def protected_resource_md(request: Request) -> Response:
        return JSONResponse(protected_resource_metadata(_settings()))

    async def authorization_server_md(request: Request) -> Response:
        return JSONResponse(authorization_server_metadata(_settings()))

    async def register(request: Request) -> Response:
        """Minimal dynamic client registration (RFC 7591) for public clients."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON => invalid request
            body = {}
        if not isinstance(body, dict):
            body = {}
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list):
            redirect_uris = []
        client_id = "mcp-client-" + secrets.token_urlsafe(8)
        response = {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": body.get("client_name") or "MCP client",
            "scope": body.get("scope") or DEFAULT_SCOPE,
        }
        return JSONResponse(response, status_code=201)

    async def authorize_get(request: Request) -> Response:
        """Render the single-user login/consent form."""
        q = request.query_params
        return _login_form(
            client_id=q.get("client_id", ""),
            redirect_uri=q.get("redirect_uri", ""),
            state=q.get("state", ""),
            scope=q.get("scope") or DEFAULT_SCOPE,
            code_challenge=q.get("code_challenge", ""),
            code_challenge_method=q.get("code_challenge_method") or "S256",
        )

    async def authorize_post(request: Request) -> Response:
        """Verify the login and redirect back with an authorization code."""
        form = await request.form()
        redirect_uri = str(form.get("redirect_uri", ""))
        client_id = str(form.get("client_id", ""))
        state = str(form.get("state", ""))
        scope = str(form.get("scope") or DEFAULT_SCOPE)
        code_challenge = str(form.get("code_challenge", "")) or None
        code_challenge_method = str(form.get("code_challenge_method") or "S256")
        email = str(form.get("email", ""))
        secret = str(form.get("secret", ""))

        if not redirect_uri:
            return _oauth_error(
                "invalid_request", "redirect_uri is required", 400
            )
        try:
            record = _provider().create_authorization_code(
                email=email,
                secret=secret,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
            )
        except OAuthProviderError as exc:
            if exc.error == "access_denied":
                # Re-render the form so the user can retry credentials.
                return _login_form(
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=state,
                    scope=scope,
                    code_challenge=code_challenge or "",
                    code_challenge_method=code_challenge_method,
                    message=exc.description or "Authorization denied.",
                )
            return _oauth_error(exc.error, exc.description, 400)

        return _redirect_with_code(
            redirect_uri, record.code.get_secret_value(), state
        )

    def _token_response(access: Any, refresh: Any) -> JSONResponse:
        """Build the RFC 6749 token response carrying access + refresh tokens."""
        return JSONResponse(
            {
                "access_token": access.token.get_secret_value(),
                "token_type": "Bearer",
                "expires_in": _settings().oauth_token_ttl_seconds,
                "refresh_token": refresh.token.get_secret_value(),
                "scope": access.scope,
            }
        )

    async def token(request: Request) -> Response:
        """Issue tokens for the ``authorization_code`` or ``refresh_token`` grant.

        ``authorization_code`` exchanges a PKCE-bound code for an access +
        refresh token pair. ``refresh_token`` exchanges a still-valid refresh
        token for a fresh pair (sliding expiration), so an actively used
        connector renews indefinitely without re-authenticating.
        """
        form = await request.form()
        grant_type = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", "")) or None
        provider = _provider()

        if grant_type == "authorization_code":
            try:
                access = provider.exchange_code(
                    code=str(form.get("code", "")),
                    client_id=client_id or "",
                    redirect_uri=str(form.get("redirect_uri", "")),
                    code_verifier=str(form.get("code_verifier", "")) or None,
                )
                refresh = provider.issue_refresh_token(
                    email=access.claims.sub,
                    client_id=client_id,
                    scope=access.scope,
                )
            except OAuthProviderError as exc:
                status = 401 if exc.error in {"invalid_client", "access_denied"} else 400
                return _oauth_error(exc.error, exc.description, status)
            return _token_response(access, refresh)

        if grant_type == "refresh_token":
            try:
                access, refresh = provider.redeem_refresh_token(
                    refresh_token=str(form.get("refresh_token", "")),
                    client_id=client_id,
                )
            except OAuthProviderError as exc:
                status = 401 if exc.error in {"invalid_client", "access_denied"} else 400
                return _oauth_error(exc.error, exc.description, status)
            return _token_response(access, refresh)

        return _oauth_error(
            "unsupported_grant_type",
            "only authorization_code and refresh_token are supported",
            400,
        )

    return [
        Route(
            PROTECTED_RESOURCE_METADATA_PATH,
            protected_resource_md,
            methods=["GET"],
        ),
        Route(
            AUTHORIZATION_SERVER_METADATA_PATH,
            authorization_server_md,
            methods=["GET"],
        ),
        Route(REGISTRATION_PATH, register, methods=["POST"]),
        Route(AUTHORIZATION_PATH, authorize_get, methods=["GET"]),
        Route(AUTHORIZATION_PATH, authorize_post, methods=["POST"]),
        Route(TOKEN_PATH, token, methods=["POST"]),
    ]


__all__ = ["build_oauth_routes"]
