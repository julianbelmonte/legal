"""OAuth discovery metadata for the remote MCP server.

Remote MCP clients (e.g. Claude Cowork) discover how to authenticate by fetching
two well-known JSON documents:

- **Protected Resource Metadata** (RFC 9728) at
  ``/.well-known/oauth-protected-resource`` — advertises the MCP resource URL and
  the authorization server(s) that mint tokens for it.
- **Authorization Server Metadata** (RFC 8414) at
  ``/.well-known/oauth-authorization-server`` — advertises the authorization,
  token, and registration endpoints plus supported flows.

Both documents derive entirely from :class:`~server.settings.McpSettings`
(``LEGAL_MCP_PUBLIC_URL`` and the issuer), recomputed from
:func:`~server.settings.get_mcp_settings` at call time. Changing the public
URL (e.g. rotating an ngrok tunnel) is reflected with no code change. The
concrete endpoint paths defined here are the contract that steps 19/20 mount.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from server.auth.models import DEFAULT_SCOPE
from server.settings import McpSettings, get_mcp_settings

# Path (relative to the public origin) of the unauthenticated branding icon.
ICON_PATH = "/icon.png"


def _origin(url: str) -> str:
    """Return the scheme://host[:port] origin of ``url`` (path stripped)."""
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rstrip("/")

# Concrete OAuth endpoint paths, appended to the issuer. Steps 19/20 mount the
# routes at exactly these paths so discovery and serving stay consistent.
AUTHORIZATION_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTRATION_PATH = "/oauth/register"

# Well-known discovery document paths (relative to the public origin).
PROTECTED_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server"


def _join(base: str, path: str) -> str:
    """Join an issuer/origin ``base`` with an absolute ``path`` safely."""
    return base.rstrip("/") + path


def protected_resource_metadata(
    settings: McpSettings | None = None,
) -> dict[str, object]:
    """Return RFC 9728 protected-resource metadata as a JSON-ready dict.

    The ``resource`` is the MCP resource URL (``LEGAL_MCP_PUBLIC_URL``, which
    ends in ``/mcp``) and ``authorization_servers`` lists the OAuth issuer.
    """
    settings = settings or get_mcp_settings()
    issuer = settings.issuer()
    return {
        "resource": settings.resource(),
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [DEFAULT_SCOPE],
        "resource_documentation": _join(issuer, "/docs"),
    }


def authorization_server_metadata(
    settings: McpSettings | None = None,
) -> dict[str, object]:
    """Return RFC 8414 authorization-server metadata as a JSON-ready dict.

    Every endpoint URL derives from the issuer so rotating the public URL needs
    no code change. Advertises the PKCE authorization-code flow used by the
    single-user provider, plus dynamic client registration.
    """
    settings = settings or get_mcp_settings()
    issuer = settings.issuer()
    return {
        "issuer": issuer,
        "authorization_endpoint": _join(issuer, AUTHORIZATION_PATH),
        "token_endpoint": _join(issuer, TOKEN_PATH),
        "registration_endpoint": _join(issuer, REGISTRATION_PATH),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [DEFAULT_SCOPE],
        # Branding shown by clients that render the authorization server's logo.
        "logo_uri": _origin(issuer) + ICON_PATH,
    }


__all__ = [
    "AUTHORIZATION_PATH",
    "AUTHORIZATION_SERVER_METADATA_PATH",
    "PROTECTED_RESOURCE_METADATA_PATH",
    "REGISTRATION_PATH",
    "TOKEN_PATH",
    "authorization_server_metadata",
    "protected_resource_metadata",
]
