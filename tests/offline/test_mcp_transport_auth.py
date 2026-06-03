"""Offline tests for the MCP transport bearer-auth guard.

Exercises :class:`server.auth.transport.BearerAuthMiddleware` directly with
a minimal Starlette app and TestClient (starlette ships with fastapi). Verifies
the 401 + ``WWW-Authenticate`` challenge for every rejected token shape and that
``/healthz`` plus the OAuth metadata endpoints stay reachable unauthenticated.
"""

from __future__ import annotations

import time

import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from server.auth.metadata import (
    PROTECTED_RESOURCE_METADATA_PATH,
    authorization_server_metadata,
    protected_resource_metadata,
)
from server.auth.models import TokenClaims
from server.auth.provider import SingleUserOAuthProvider
from server.auth.transport import (
    BearerAuthMiddleware,
    build_www_authenticate,
    extract_bearer_token,
    get_token_claims,
)
from server.settings import McpSettings

EMAIL = "user@example.com"
SECRET = "test-secret"
SIGNING_KEY = "test-signing-key"
PUBLIC_URL = "http://127.0.0.1:8080/mcp"
ISSUER = "http://127.0.0.1:8080"
_JWT_ALG = "HS256"


def _settings(**overrides: object) -> McpSettings:
    base: dict[str, object] = {
        "public_url": PUBLIC_URL,
        "oauth_issuer": ISSUER,
        "allowed_emails": EMAIL,
        "oauth_login_secret": SECRET,
        "oauth_signing_key": SIGNING_KEY,
    }
    base.update(overrides)
    return McpSettings(**base)


def _provider(settings: McpSettings | None = None) -> SingleUserOAuthProvider:
    return SingleUserOAuthProvider.for_settings(settings or _settings())


def _build_client(settings: McpSettings | None = None) -> TestClient:
    settings = settings or _settings()
    provider = _provider(settings)

    async def mcp_endpoint(request):  # type: ignore[no-untyped-def]
        claims = get_token_claims(request.scope)
        sub = claims.sub if claims else None
        return JSONResponse({"ok": True, "sub": sub})

    async def healthz(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    async def resource_md(request):  # type: ignore[no-untyped-def]
        return JSONResponse(protected_resource_metadata(settings))

    async def server_md(request):  # type: ignore[no-untyped-def]
        return JSONResponse(authorization_server_metadata(settings))

    app = Starlette(
        routes=[
            Route("/mcp", mcp_endpoint, methods=["GET", "POST"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route(PROTECTED_RESOURCE_METADATA_PATH, resource_md, methods=["GET"]),
            Route(
                "/.well-known/oauth-authorization-server",
                server_md,
                methods=["GET"],
            ),
        ]
    )
    wrapped = BearerAuthMiddleware(app, provider=provider, settings=settings)
    return TestClient(wrapped)


def _valid_token(settings: McpSettings | None = None) -> str:
    settings = settings or _settings()
    token = _provider(settings).issue_access_token(email=EMAIL)
    return token.token.get_secret_value()


def _signed(**claims: object) -> str:
    """Sign an arbitrary claim set with the test signing key."""
    return jwt.encode(claims, SIGNING_KEY, algorithm=_JWT_ALG)


# --- helper unit tests -------------------------------------------------------


def test_mcp_auth_extract_bearer_token() -> None:
    assert extract_bearer_token([(b"authorization", b"Bearer abc")]) == "abc"
    assert extract_bearer_token([(b"Authorization", b"bearer xyz")]) == "xyz"
    assert extract_bearer_token([(b"authorization", b"Basic abc")]) is None
    assert extract_bearer_token([(b"authorization", b"Bearer")]) is None
    assert extract_bearer_token([]) is None


def test_mcp_auth_www_authenticate_challenge_points_at_metadata() -> None:
    challenge = build_www_authenticate(
        resource_metadata=ISSUER + PROTECTED_RESOURCE_METADATA_PATH,
        error="invalid_token",
        error_description="bad",
    )
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{ISSUER}{PROTECTED_RESOURCE_METADATA_PATH}"' in challenge
    assert 'error="invalid_token"' in challenge


# --- transport guard: rejection paths ---------------------------------------


def test_mcp_auth_missing_token_returns_401_with_challenge() -> None:
    client = _build_client()
    resp = client.get("/mcp")
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert PROTECTED_RESOURCE_METADATA_PATH in www
    assert ISSUER in www


def test_mcp_auth_invalid_token_returns_401() -> None:
    client = _build_client()
    resp = client.get("/mcp", headers={"authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers["www-authenticate"]


def test_mcp_auth_expired_token_rejected() -> None:
    settings = _settings()
    past = int(time.time()) - 10
    token = _signed(
        sub=EMAIL,
        aud=settings.resource(),
        iss=settings.issuer(),
        iat=past - 100,
        exp=past,
    )
    client = _build_client(settings)
    resp = client.get("/mcp", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_mcp_auth_wrong_audience_rejected() -> None:
    settings = _settings()
    token = _signed(
        sub=EMAIL,
        aud="https://evil.example/mcp",
        iss=settings.issuer(),
        iat=int(time.time()),
        exp=int(time.time()) + 600,
    )
    client = _build_client(settings)
    resp = client.get("/mcp", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_mcp_auth_wrong_issuer_rejected() -> None:
    settings = _settings()
    token = _signed(
        sub=EMAIL,
        aud=settings.resource(),
        iss="https://evil.example",
        iat=int(time.time()),
        exp=int(time.time()) + 600,
    )
    client = _build_client(settings)
    resp = client.get("/mcp", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_mcp_auth_non_allowlisted_subject_rejected() -> None:
    settings = _settings()
    token = _signed(
        sub="intruder@example.com",
        aud=settings.resource(),
        iss=settings.issuer(),
        iat=int(time.time()),
        exp=int(time.time()) + 600,
    )
    client = _build_client(settings)
    resp = client.get("/mcp", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_mcp_auth_wrong_signing_key_rejected() -> None:
    settings = _settings()
    token = jwt.encode(
        {
            "sub": EMAIL,
            "aud": settings.resource(),
            "iss": settings.issuer(),
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        "a-different-key",
        algorithm=_JWT_ALG,
    )
    client = _build_client(settings)
    resp = client.get("/mcp", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# --- transport guard: success + open endpoints ------------------------------


def test_mcp_auth_valid_token_allowed() -> None:
    client = _build_client()
    resp = client.get("/mcp", headers={"authorization": f"Bearer {_valid_token()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["sub"] == EMAIL


def test_mcp_auth_healthz_open_without_token() -> None:
    client = _build_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert "www-authenticate" not in resp.headers


def test_mcp_auth_metadata_endpoints_open_without_token() -> None:
    client = _build_client()
    pr = client.get(PROTECTED_RESOURCE_METADATA_PATH)
    az = client.get("/.well-known/oauth-authorization-server")
    assert pr.status_code == 200
    assert az.status_code == 200
    assert pr.json()["resource"] == PUBLIC_URL


def test_mcp_auth_disabled_allows_unauthenticated_mcp() -> None:
    settings = _settings(auth_enabled=False)
    client = _build_client(settings)
    resp = client.get("/mcp")
    assert resp.status_code == 200


def test_mcp_auth_claims_available_on_scope() -> None:
    settings = _settings()
    provider = _provider(settings)
    claims = provider.decode_token(_valid_token(settings))
    assert isinstance(claims, TokenClaims)
    assert claims.sub == EMAIL


@pytest.mark.parametrize("path", ["/healthz", PROTECTED_RESOURCE_METADATA_PATH])
def test_mcp_auth_open_paths_never_challenge(path: str) -> None:
    client = _build_client()
    resp = client.get(path)
    assert resp.status_code == 200
