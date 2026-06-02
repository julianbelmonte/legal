"""Offline tests for the MCP OAuth token, client, and claim models."""

from __future__ import annotations

from mcp_server.auth.models import (
    AccessToken,
    AllowedUser,
    AuthorizationCode,
    OAuthError,
    RegisteredClient,
    TokenClaims,
)


def test_token_claims_minimal() -> None:
    c = TokenClaims(
        sub="user@example.com", aud="http://127.0.0.1:8080/mcp", iss="test"
    )
    assert c.sub == "user@example.com"
    assert c.aud == "http://127.0.0.1:8080/mcp"
    assert c.iss == "test"
    assert c.scope == "mcp"
    assert c.is_expired() is False


def test_token_claims_build_sets_expiry_and_email() -> None:
    c = TokenClaims.build(
        sub="user@example.com",
        aud="aud",
        iss="iss",
        ttl_seconds=3600,
        issued_at=1000,
        client_id="client-1",
    )
    assert c.exp == 4600
    assert c.email == "user@example.com"
    assert c.client_id == "client-1"
    assert c.is_expired(now=5000) is True
    assert c.is_expired(now=2000) is False
    payload = c.to_payload()
    assert payload["sub"] == "user@example.com"
    assert "nbf" not in payload  # None values omitted


def test_secrets_masked_in_repr() -> None:
    code = AuthorizationCode(
        code="super-secret-code",
        client_id="c1",
        redirect_uri="https://app/cb",
        user_email="user@example.com",
    )
    assert "super-secret-code" not in repr(code)
    assert code.code.get_secret_value() == "super-secret-code"

    client = RegisteredClient(client_id="c1", client_secret="topsecret")
    assert "topsecret" not in repr(client)
    assert client.is_public is False

    public = RegisteredClient(
        client_id="c2", redirect_uris=["https://app/cb"]
    )
    assert public.is_public is True
    assert public.allows_redirect("https://app/cb") is True
    assert public.allows_redirect("https://evil/cb") is False


def test_access_token_expiry() -> None:
    claims = TokenClaims(sub="u@e.com", aud="aud", iss="iss")
    token = AccessToken(token="abc", claims=claims, expires_at=1000)
    assert token.is_expired(now=2000) is True
    assert token.is_expired(now=500) is False
    assert "abc" not in repr(token)


def test_allowed_user_normalization() -> None:
    assert AllowedUser(email="  User@Example.COM ").normalized_email == (
        "user@example.com"
    )


def test_oauth_error_payload_omits_none() -> None:
    err = OAuthError(error="invalid_token", error_description="bad token")
    payload = err.to_payload()
    assert payload == {"error": "invalid_token", "error_description": "bad token"}
