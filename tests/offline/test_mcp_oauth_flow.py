"""End-to-end OAuth flow tests for the combined ASGI app.

These tests drive the full OAuth surface of :func:`api.main.create_app` through
a Starlette ``TestClient`` (the combined app mounts ``/healthz``, the OAuth
discovery + flow endpoints, and the bearer-protected ``/mcp`` transport). Unlike
the unit tests in ``test_mcp_transport_auth`` / ``test_mcp_oauth_provider`` —
which exercise the middleware and provider in isolation — these prove the real
end-to-end behavior Claude Cowork depends on:

- the unauthenticated ``/mcp`` challenge (401 + ``WWW-Authenticate``);
- the protected-resource and authorization-server metadata documents;
- allowed-user authorization (login secret + PKCE) minting an auth code;
- rejection of non-allowlisted users and disallowed redirect URIs;
- PKCE verification on token exchange (wrong vs. correct ``code_verifier``);
- token expiry rejection by the bearer guard;
- a valid bearer token passing the guard (status != 401).

The combined app reads OAuth config from process env through ``get_mcp_settings``.
Each test sets the required env via ``monkeypatch.setenv`` and calls
``reload_mcp_settings`` *before* building the app so the in-test config takes
effect; the acceptance command also sets these at process level.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.testclient import TestClient

from mcp_server.auth.metadata import (
    AUTHORIZATION_PATH,
    AUTHORIZATION_SERVER_METADATA_PATH,
    PROTECTED_RESOURCE_METADATA_PATH,
    TOKEN_PATH,
)
from mcp_server.auth.provider import (
    SingleUserOAuthProvider,
    compute_s256_challenge,
)
from mcp_server.settings import get_mcp_settings, reload_mcp_settings

EMAIL = "user@example.com"
SECRET = "test-secret"
SIGNING_KEY = "test-signing-key"
PUBLIC_URL = "http://127.0.0.1:8080/mcp"
ISSUER = "http://127.0.0.1:8080"
REDIRECT_URI = "https://client.example/callback"
CLIENT_ID = "mcp-test-client"


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the OAuth env and reload the cached MCP settings.

    Pins the public URL/issuer and the redirect-URI allowlist so the flow tests
    are deterministic regardless of the host env, then clears the settings cache
    so the freshly-built app reads this configuration.
    """
    monkeypatch.setenv("LEGAL_MCP_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("LEGAL_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("LEGAL_MCP_ALLOWED_EMAILS", EMAIL)
    monkeypatch.setenv("LEGAL_MCP_OAUTH_LOGIN_SECRET", SECRET)
    monkeypatch.setenv("LEGAL_MCP_OAUTH_SIGNING_KEY", SIGNING_KEY)
    monkeypatch.setenv("LEGAL_MCP_OAUTH_REDIRECT_URIS", REDIRECT_URI)
    reload_mcp_settings()
    yield
    # Restore the default cached settings for other tests.
    reload_mcp_settings()


@pytest.fixture
def client(oauth_env: None) -> TestClient:
    """Build the combined ASGI app *after* the OAuth env is in effect."""
    # Imported lazily so module import does not snapshot settings before the env
    # fixture runs.
    from api.main import create_app

    app = create_app()
    # ``raise_server_exceptions=False`` lets us observe the MCP transport's own
    # status codes (e.g. 400/406) instead of surfacing them as test errors.
    return TestClient(app, raise_server_exceptions=False)


def _provider() -> SingleUserOAuthProvider:
    """Build a provider from the live settings (same config as the app)."""
    return SingleUserOAuthProvider.for_settings(get_mcp_settings())


def _authorize(
    client: TestClient,
    *,
    email: str,
    secret: str,
    redirect_uri: str,
    code_challenge: str | None,
):
    """POST the login form and return the (non-following) authorize response."""
    form = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "state": "xyz-state",
        "scope": "mcp",
        "code_challenge_method": "S256",
        "email": email,
        "secret": secret,
    }
    if code_challenge is not None:
        form["code_challenge"] = code_challenge
    return client.post(AUTHORIZATION_PATH, data=form, follow_redirects=False)


def _extract_code(response) -> str:
    """Pull the ``code`` query param out of a 302 redirect ``Location``."""
    from urllib.parse import parse_qs, urlparse

    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["code"][0]


# --- unauthenticated MCP challenge ------------------------------------------


def test_oauth_unauthenticated_mcp_returns_401_challenge(client: TestClient) -> None:
    """An unauthenticated /mcp call eventually yields 401 + WWW-Authenticate."""
    # ``/mcp`` may 307-redirect to ``/mcp/`` first; follow to the eventual 401.
    resp = client.get("/mcp", follow_redirects=True)
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert PROTECTED_RESOURCE_METADATA_PATH in www


# --- discovery metadata ------------------------------------------------------


def test_oauth_protected_resource_metadata(client: TestClient) -> None:
    resp = client.get(PROTECTED_RESOURCE_METADATA_PATH)
    assert resp.status_code == 200
    md = resp.json()
    assert md["resource"].endswith("/mcp")
    assert md["resource"] == PUBLIC_URL
    assert md["authorization_servers"] == [ISSUER]


def test_oauth_authorization_server_metadata(client: TestClient) -> None:
    resp = client.get(AUTHORIZATION_SERVER_METADATA_PATH)
    assert resp.status_code == 200
    md = resp.json()
    assert md["issuer"] == ISSUER
    assert md["authorization_endpoint"] == ISSUER + AUTHORIZATION_PATH
    assert md["token_endpoint"] == ISSUER + TOKEN_PATH
    assert md["code_challenge_methods_supported"] == ["S256"]


# --- authorization (login + allowlist + redirect validation) ----------------


def test_oauth_allowed_user_authorization_issues_code(client: TestClient) -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = compute_s256_challenge(verifier)
    resp = _authorize(
        client,
        email=EMAIL,
        secret=SECRET,
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(REDIRECT_URI)
    assert "code=" in location
    assert "state=xyz-state" in location


def test_oauth_non_allowlisted_user_rejected(client: TestClient) -> None:
    """A non-allowlisted email re-renders the form (no redirect, no code)."""
    resp = _authorize(
        client,
        email="intruder@example.com",
        secret=SECRET,
        redirect_uri=REDIRECT_URI,
        code_challenge=compute_s256_challenge(secrets.token_urlsafe(48)),
    )
    # access_denied re-renders the login form (200 HTML), never a code redirect.
    assert resp.status_code == 200
    assert "location" not in resp.headers


def test_oauth_wrong_login_secret_rejected(client: TestClient) -> None:
    resp = _authorize(
        client,
        email=EMAIL,
        secret="wrong-secret",
        redirect_uri=REDIRECT_URI,
        code_challenge=compute_s256_challenge(secrets.token_urlsafe(48)),
    )
    assert resp.status_code == 200
    assert "location" not in resp.headers


def test_oauth_invalid_redirect_uri_rejected(client: TestClient) -> None:
    resp = _authorize(
        client,
        email=EMAIL,
        secret=SECRET,
        redirect_uri="https://evil.example/callback",
        code_challenge=compute_s256_challenge(secrets.token_urlsafe(48)),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_request"


# --- token exchange + PKCE verification -------------------------------------


def test_oauth_token_exchange_wrong_verifier_rejected(client: TestClient) -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = compute_s256_challenge(verifier)
    auth = _authorize(
        client,
        email=EMAIL,
        secret=SECRET,
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
    )
    code = _extract_code(auth)

    resp = client.post(
        TOKEN_PATH,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": "this-is-the-wrong-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_oauth_token_exchange_correct_verifier_succeeds(
    client: TestClient,
) -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = compute_s256_challenge(verifier)
    auth = _authorize(
        client,
        email=EMAIL,
        secret=SECRET,
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
    )
    code = _extract_code(auth)

    resp = client.post(
        TOKEN_PATH,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    token = body["access_token"]
    assert token
    # The issued token must validate against the live provider/settings.
    claims = _provider().decode_token(token)
    assert claims.sub == EMAIL


# --- token expiry ------------------------------------------------------------


def test_oauth_expired_token_rejected_by_bearer_guard(client: TestClient) -> None:
    """An expired access token is rejected by the /mcp bearer guard (401)."""
    provider = _provider()
    # Issue a token whose TTL has already lapsed by anchoring ``now`` far in the
    # past (exp = past + ttl is still in the past).
    expired = provider.issue_access_token(email=EMAIL, now=10_000)
    token = expired.token.get_secret_value()
    # Provider-level: the verifier rejects it.
    assert provider.verify_token_sync(token) is None
    # End-to-end: the bearer guard rejects it on /mcp.
    resp = client.get(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        follow_redirects=True,
    )
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers["www-authenticate"]


# --- successful bearer-authenticated MCP access -----------------------------


def test_oauth_valid_bearer_token_passes_guard(client: TestClient) -> None:
    """A valid bearer token passes the guard: the response is NOT a 401.

    The MCP transport may answer a non-MCP request with its own status (e.g.
    400/406), but a valid token must never produce the 401 auth challenge.
    """
    token = _provider().issue_access_token(email=EMAIL).token.get_secret_value()
    resp = client.post(
        "/mcp",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        follow_redirects=True,
    )
    assert resp.status_code != 401
