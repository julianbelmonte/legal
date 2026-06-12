"""Offline tests for deploy/domain.py — the bare-domain URL/env/DNS helpers.

The load-bearing invariant: the public URL, OAuth issuer, and OAuth resource
are all the **bare domain** (``https://<domain>``), with no ``/mcp`` suffix, so
the advertised OAuth metadata matches the Caddy-served connector URL.
"""

from __future__ import annotations

import pytest

from deploy.domain import (
    MCP_OAUTH_ISSUER_ENV_VAR,
    MCP_PUBLIC_URL_ENV_VAR,
    dns_host_label,
    oauth_env_updates_for_domain,
    public_url_for_domain,
)


def test_public_url_is_bare_domain_no_mcp() -> None:
    url = public_url_for_domain("mcp.arglegal.live")
    assert url == "https://mcp.arglegal.live"
    assert not url.endswith("/mcp")


def test_oauth_env_issuer_equals_public_equals_bare_domain() -> None:
    env = oauth_env_updates_for_domain("mcp.arglegal.live")
    assert env[MCP_PUBLIC_URL_ENV_VAR] == "https://mcp.arglegal.live"
    assert env[MCP_OAUTH_ISSUER_ENV_VAR] == "https://mcp.arglegal.live"
    # The invariant: issuer == public URL == bare domain (NOT the /mcp form).
    assert env[MCP_PUBLIC_URL_ENV_VAR] == env[MCP_OAUTH_ISSUER_ENV_VAR]
    # No /mcp *path suffix* (the domain itself starts with "mcp." so a substring
    # check would false-positive on "https://mcp..."); guard the path end.
    assert not env[MCP_PUBLIC_URL_ENV_VAR].endswith("/mcp")


@pytest.mark.parametrize(
    ("domain", "expected_host"),
    [
        ("mcp.arglegal.live", "mcp"),
        ("arglegal.live", "@"),
        ("www.example.org", "www"),
    ],
)
def test_dns_host_label(domain: str, expected_host: str) -> None:
    assert dns_host_label(domain) == expected_host


def test_empty_domain_rejected() -> None:
    for bad in ("", "   ", "/"):
        with pytest.raises(ValueError):
            public_url_for_domain(bad)
