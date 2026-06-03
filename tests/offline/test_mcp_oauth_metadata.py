"""Offline tests for MCP OAuth discovery metadata (RFC 8414 / RFC 9728)."""

from __future__ import annotations

from mcp_server.auth.metadata import (
    AUTHORIZATION_PATH,
    REGISTRATION_PATH,
    TOKEN_PATH,
    authorization_server_metadata,
    protected_resource_metadata,
)
from mcp_server.settings import McpSettings

PUBLIC_URL = "https://example.ngrok.app/mcp"
ISSUER = "https://example.ngrok.app"


def _settings() -> McpSettings:
    return McpSettings(public_url=PUBLIC_URL, oauth_issuer=ISSUER)


def test_protected_resource_metadata_shape() -> None:
    md = protected_resource_metadata(_settings())
    assert md["resource"].endswith("/mcp")
    assert md["resource"] == PUBLIC_URL
    assert md["authorization_servers"] == [ISSUER]
    assert "header" in md["bearer_methods_supported"]
    assert md["scopes_supported"]


def test_authorization_server_metadata_shape() -> None:
    md = authorization_server_metadata(_settings())
    assert md["issuer"] == ISSUER
    assert md["authorization_endpoint"] == ISSUER + AUTHORIZATION_PATH
    assert md["token_endpoint"] == ISSUER + TOKEN_PATH
    assert md["registration_endpoint"] == ISSUER + REGISTRATION_PATH
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in md["grant_types_supported"]
    assert md["response_types_supported"] == ["code"]
    # Branding logo derives from the issuer origin (path stripped).
    assert md["logo_uri"] == ISSUER + "/icon.png"


def test_endpoints_derive_from_issuer_no_caching() -> None:
    """Endpoints follow the issuer so an ngrok URL change needs no code change."""
    other = McpSettings(
        public_url="https://new.ngrok.app/mcp",
        oauth_issuer="https://new.ngrok.app",
    )
    md = authorization_server_metadata(other)
    assert md["authorization_endpoint"].startswith("https://new.ngrok.app/")
    assert protected_resource_metadata(other)["resource"] == "https://new.ngrok.app/mcp"


def test_issuer_defaults_to_public_url_origin() -> None:
    settings = McpSettings(public_url=PUBLIC_URL)  # no explicit issuer
    pr = protected_resource_metadata(settings)
    az = authorization_server_metadata(settings)
    # issuer() falls back to the full public_url; endpoints still derive from it.
    assert az["issuer"] == pr["authorization_servers"][0]
    assert az["token_endpoint"].endswith(TOKEN_PATH)
