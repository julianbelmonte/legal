"""OAuth authentication for the MCP server.

The remote MCP endpoint is public HTTPS but protected by OAuth bearer tokens
with a single-user allowlist. This package owns OAuth settings glue, token
models (:mod:`server.auth.models`), the single-user provider, the
discovery/metadata endpoints, and bearer validation. Models land here first
(step 16); the provider and endpoints attach in later steps.

The token/claim/client/error models are re-exported here so callers can use
either ``server.auth.models`` or ``server.auth`` directly.
"""

from __future__ import annotations

from server.auth.metadata import (
    authorization_server_metadata,
    protected_resource_metadata,
)
from server.auth.models import (
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
from server.auth.provider import (
    OAuthProviderError,
    SingleUserOAuthProvider,
    compute_s256_challenge,
)

__all__ = [
    "DEFAULT_SCOPE",
    "AccessToken",
    "AllowedUser",
    "AuthorizationCode",
    "OAuthError",
    "OAuthErrorCode",
    "OAuthProviderError",
    "RefreshToken",
    "RegisteredClient",
    "SingleUserOAuthProvider",
    "TokenClaims",
    "authorization_server_metadata",
    "compute_s256_challenge",
    "protected_resource_metadata",
]
