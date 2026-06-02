"""Offline tests for legal_deploy.ngrok URL helpers and tunnel discovery."""

from __future__ import annotations

import httpx
import pytest

from legal_deploy import ngrok


# --- mcp_url_from_tunnel_url ------------------------------------------------


def test_mcp_url_appends_mcp():
    assert (
        ngrok.mcp_url_from_tunnel_url("https://example.ngrok.app")
        == "https://example.ngrok.app/mcp"
    )


def test_mcp_url_normalizes_trailing_slash():
    assert (
        ngrok.mcp_url_from_tunnel_url("https://example.ngrok.app/")
        == "https://example.ngrok.app/mcp"
    )


def test_mcp_url_is_idempotent_for_existing_mcp_suffix():
    assert (
        ngrok.mcp_url_from_tunnel_url("https://example.ngrok.app/mcp")
        == "https://example.ngrok.app/mcp"
    )
    assert (
        ngrok.mcp_url_from_tunnel_url("https://example.ngrok.app/mcp/")
        == "https://example.ngrok.app/mcp"
    )


def test_mcp_url_preserves_http_scheme():
    assert (
        ngrok.mcp_url_from_tunnel_url("http://127.0.0.1:8080")
        == "http://127.0.0.1:8080/mcp"
    )


def test_mcp_url_rejects_empty():
    with pytest.raises(ValueError):
        ngrok.mcp_url_from_tunnel_url("   ")


# --- tunnel_base_from_url ---------------------------------------------------


def test_tunnel_base_strips_mcp_suffix():
    assert (
        ngrok.tunnel_base_from_url("https://example.ngrok.app/mcp/")
        == "https://example.ngrok.app"
    )
    assert (
        ngrok.tunnel_base_from_url("https://example.ngrok.app")
        == "https://example.ngrok.app"
    )


# --- render commands (never leak the authtoken) -----------------------------


def test_render_start_command_omits_authtoken():
    cmd = ngrok.render_start_command(8080)
    assert cmd == "ngrok http 8080 --log=stdout"
    assert "authtoken" not in cmd
    assert "secret-token" not in cmd


def test_render_start_command_rejects_bad_port():
    with pytest.raises(ValueError):
        ngrok.render_start_command(0)


def test_render_authtoken_command_quotes_token():
    cmd = ngrok.render_authtoken_command("tok with spaces")
    assert cmd.startswith("ngrok config add-authtoken ")
    assert "tok with spaces" in cmd


# --- discover_public_url with a mocked httpx response -----------------------


def _client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_prefers_https_tunnel():
    payload = {
        "tunnels": [
            {"public_url": "http://example.ngrok.app", "proto": "http"},
            {"public_url": "https://example.ngrok.app", "proto": "https"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tunnels"
        return httpx.Response(200, json=payload)

    with _client_for(handler) as client:
        url = ngrok.discover_public_url(client=client)
    assert url == "https://example.ngrok.app"


def test_discover_falls_back_to_only_tunnel():
    payload = {"tunnels": [{"public_url": "http://example.ngrok.app"}]}

    with _client_for(lambda r: httpx.Response(200, json=payload)) as client:
        url = ngrok.discover_public_url(client=client)
    assert url == "http://example.ngrok.app"


def test_discover_raises_when_no_tunnels():
    with _client_for(lambda r: httpx.Response(200, json={"tunnels": []})) as client:
        with pytest.raises(ngrok.NgrokError):
            ngrok.discover_public_url(client=client)


def test_discover_raises_on_http_error():
    with _client_for(lambda r: httpx.Response(502)) as client:
        with pytest.raises(ngrok.NgrokError):
            ngrok.discover_public_url(client=client)


# --- oauth_env_updates ------------------------------------------------------


def test_oauth_env_updates_maps_public_url_and_issuer():
    updates = ngrok.oauth_env_updates("https://example.ngrok.app/")
    assert updates == {
        "LEGAL_MCP_PUBLIC_URL": "https://example.ngrok.app/mcp",
        "LEGAL_MCP_OAUTH_ISSUER": "https://example.ngrok.app",
    }
