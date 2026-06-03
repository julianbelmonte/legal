"""Offline tests for the MCP server branding icon.

The server advertises a ``serverInfo.icons`` entry (modern MCP handshake) and
serves the asset unauthenticated at ``/icon.png`` under the public origin so MCP
clients (e.g. Claude) can render it for the connector.
"""

from __future__ import annotations

from starlette.testclient import TestClient

import api.main as api_main
from server.main import (
    ICON_FILE,
    ICON_ROUTE_PATH,
    build_mcp_server,
    icon_url,
    public_origin,
    server_icons,
)
from server.settings import McpSettings

PUBLIC_URL = "https://mcp.example.test/mcp"


def _settings() -> McpSettings:
    return McpSettings(
        public_url=PUBLIC_URL,
        oauth_issuer="https://mcp.example.test",
        auth_enabled=False,
    )


def test_icon_asset_exists() -> None:
    assert ICON_FILE.is_file()
    assert ICON_FILE.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_public_origin_strips_mcp_path() -> None:
    assert public_origin(_settings()) == "https://mcp.example.test"
    assert icon_url(_settings()) == "https://mcp.example.test" + ICON_ROUTE_PATH


def test_server_advertises_icons() -> None:
    icons = server_icons(_settings())
    assert len(icons) == 1
    assert icons[0].src == "https://mcp.example.test/icon.png"
    assert icons[0].mimeType == "image/png"

    server = build_mcp_server(_settings())
    advertised = server._mcp_server.icons or []
    assert [i.src for i in advertised] == [icons[0].src]
    assert server._mcp_server.website_url == "https://mcp.example.test"


def test_icon_route_served_unauthenticated() -> None:
    """The deployed app serves the icon at /icon.png without a bearer token."""
    app = api_main.create_app()
    with TestClient(app) as client:
        resp = client.get(ICON_ROUTE_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert resp.content == ICON_FILE.read_bytes()
