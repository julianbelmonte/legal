"""OAuth authentication for the MCP server.

The remote MCP endpoint is public HTTPS but protected by OAuth bearer tokens
with a single-user allowlist. This package owns OAuth settings glue, token
models (:mod:`mcp_server.auth.models`), the single-user provider, the
discovery/metadata endpoints, and bearer validation. Models land here first
(step 16); the provider and endpoints attach in later steps.

The token/claim/client/error models are re-exported here so callers can use
either ``mcp_server.auth.models`` or ``mcp_server.auth`` directly.
"""

from __future__ import annotations

from mcp_server.auth.models import (
    DEFAULT_SCOPE,
    AccessToken,
    AllowedUser,
    AuthorizationCode,
    OAuthError,
    OAuthErrorCode,
    RefreshToken,
    RegisteredClient,
    TokenClaims,
)

__all__ = [
    "DEFAULT_SCOPE",
    "AccessToken",
    "AllowedUser",
    "AuthorizationCode",
    "OAuthError",
    "OAuthErrorCode",
    "RefreshToken",
    "RegisteredClient",
    "TokenClaims",
]
