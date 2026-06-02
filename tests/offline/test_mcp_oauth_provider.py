"""Offline tests for the single-user MCP OAuth provider."""

from __future__ import annotations

import pytest

from mcp_server.auth.provider import (
    OAuthProviderError,
    SingleUserOAuthProvider,
    compute_s256_challenge,
)
from mcp_server.settings import McpSettings

EMAIL = "user@example.com"
SECRET = "test-secret"
SIGNING_KEY = "test-signing-key"
CLIENT_ID = "client-1"
REDIRECT_URI = "https://client.example/callback"


def _provider() -> SingleUserOAuthProvider:
    settings = McpSettings(
        allowed_emails=EMAIL,
        oauth_login_secret=SECRET,
        oauth_signing_key=SIGNING_KEY,
    )
    return SingleUserOAuthProvider.for_settings(settings)


def test_for_settings_and_allowlist() -> None:
    p = _provider()
    assert p.is_allowed_user(EMAIL)
    assert p.is_allowed_user("USER@EXAMPLE.COM")  # case-insensitive
    assert not p.is_allowed_user("other@example.com")
    assert not p.is_allowed_user("")


def test_verify_login_rejects_wrong_secret_and_non_allowed() -> None:
    p = _provider()
    assert p.verify_login(EMAIL, SECRET)
    assert not p.verify_login(EMAIL, "wrong")
    assert not p.verify_login("other@example.com", SECRET)


def test_api_key_is_not_a_login_credential() -> None:
    # The legal API key must never authorize login; only the OAuth login secret.
    p = SingleUserOAuthProvider.for_settings(
        McpSettings(
            allowed_emails=EMAIL,
            oauth_login_secret=SECRET,
            oauth_signing_key=SIGNING_KEY,
        )
    )
    assert not p.verify_login(EMAIL, "legal-api-key-value")


def test_token_round_trip() -> None:
    p = _provider()
    tok = p.issue_access_token(email=EMAIL, client_id=CLIENT_ID)
    claims = p.decode_token(tok.token.get_secret_value())
    assert claims.sub == EMAIL
    assert claims.aud == p.resource
    assert claims.iss == p.issuer
    assert claims.client_id == CLIENT_ID
    assert p.verify_token_sync(tok.token.get_secret_value()) is not None


def test_authorization_code_pkce_flow() -> None:
    p = _provider()
    verifier = "a" * 64
    challenge = compute_s256_challenge(verifier)
    code = p.create_authorization_code(
        email=EMAIL,
        secret=SECRET,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
    )
    tok = p.exchange_code(
        code=code.code.get_secret_value(),
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_verifier=verifier,
    )
    assert p.decode_token(tok.token.get_secret_value()).sub == EMAIL


def test_pkce_wrong_verifier_rejected() -> None:
    p = _provider()
    challenge = compute_s256_challenge("a" * 64)
    code = p.create_authorization_code(
        email=EMAIL,
        secret=SECRET,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        code_challenge=challenge,
    )
    with pytest.raises(OAuthProviderError):
        p.exchange_code(
            code=code.code.get_secret_value(),
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
            code_verifier="b" * 64,
        )


def test_code_is_single_use() -> None:
    p = _provider()
    code = p.create_authorization_code(
        email=EMAIL,
        secret=SECRET,
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )
    p.exchange_code(
        code=code.code.get_secret_value(),
        client_id=CLIENT_ID,
        redirect_uri=REDIRECT_URI,
    )
    with pytest.raises(OAuthProviderError):
        p.exchange_code(
            code=code.code.get_secret_value(),
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        )


def test_create_code_rejects_wrong_secret() -> None:
    p = _provider()
    with pytest.raises(OAuthProviderError):
        p.create_authorization_code(
            email=EMAIL,
            secret="wrong",
            client_id=CLIENT_ID,
            redirect_uri=REDIRECT_URI,
        )


def test_token_for_non_allowed_user_rejected() -> None:
    # A token signed for a now-disallowed subject must fail verification.
    p = _provider()
    tok = p.issue_access_token(email=EMAIL, client_id=CLIENT_ID)
    raw = tok.token.get_secret_value()
    p.allowed_emails = frozenset()
    assert p.verify_token_sync(raw) is None


def test_decode_rejects_tampered_token() -> None:
    p = _provider()
    tok = p.issue_access_token(email=EMAIL, client_id=CLIENT_ID)
    raw = tok.token.get_secret_value() + "x"
    assert p.verify_token_sync(raw) is None
