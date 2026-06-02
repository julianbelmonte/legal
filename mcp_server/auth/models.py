"""OAuth token, client, and claim models for the MCP server.

The remote MCP endpoint is public HTTPS but protected by OAuth 2.1 bearer
tokens with a single-user allowlist. This module defines the pydantic models
that the OAuth provider (step 17) and the metadata/authorization endpoints use:
authorization codes, access tokens, refresh tokens, registered clients, the
allowed-user record, JWT-style :class:`TokenClaims`, and OAuth error payloads.

Design notes:

- All models are pydantic v2 ``BaseModel``s, consistent with the rest of the
  consumer layer and ``mcp_server.settings``.
- Secret material (authorization-code values, token strings, client secrets)
  uses :class:`~pydantic.SecretStr` so it is masked in ``repr``/log output and
  never leaks into tracebacks.
- :class:`TokenClaims` is a permissive JWT claim set: ``sub``/``aud``/``iss``
  are accepted directly and the usual OAuth/JWT claims (``exp``, ``iat``,
  ``scope``, ``client_id``, ``email`` ...) are optional with sensible defaults.
  Build/serialize helpers keep the provider in step 17 thin.
- Configurable issuer, resource URL, token TTL, allowed redirect URIs, and
  allowed emails live on :class:`mcp_server.settings.McpSettings`; this module
  references those rather than duplicating configuration.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# Default OAuth scope granted to the single allowed user.
DEFAULT_SCOPE = "mcp"


def _now() -> int:
    """Return the current POSIX time as an integer (seconds)."""
    return int(time.time())


class TokenClaims(BaseModel):
    """JWT-style claims carried by an issued access token.

    The three registered claims required by the resource server are accepted
    positionally by name: ``sub`` (the authenticated user, an email), ``aud``
    (the protected resource URL / MCP audience), and ``iss`` (the issuer). The
    remaining standard OAuth/JWT claims are optional with sensible defaults so a
    minimal ``TokenClaims(sub=..., aud=..., iss=...)`` is valid.
    """

    model_config = ConfigDict(extra="allow")

    sub: str = Field(description="Subject: the authenticated user (email).")
    aud: str = Field(description="Audience: the protected resource / MCP URL.")
    iss: str = Field(description="Issuer of the token.")

    iat: int = Field(default_factory=_now, description="Issued-at (POSIX seconds).")
    exp: int | None = Field(default=None, description="Expiry (POSIX seconds).")
    nbf: int | None = Field(default=None, description="Not-before (POSIX seconds).")
    jti: str | None = Field(default=None, description="JWT id (unique token id).")

    scope: str = Field(default=DEFAULT_SCOPE, description="Granted OAuth scope.")
    client_id: str | None = Field(default=None, description="OAuth client id.")
    email: str | None = Field(default=None, description="Authenticated email.")

    @classmethod
    def build(
        cls,
        *,
        sub: str,
        aud: str,
        iss: str,
        ttl_seconds: int | None = None,
        scope: str = DEFAULT_SCOPE,
        client_id: str | None = None,
        issued_at: int | None = None,
        **extra: Any,
    ) -> TokenClaims:
        """Construct claims for a freshly issued token.

        ``ttl_seconds`` (when given and positive) sets ``exp`` relative to
        ``issued_at`` (defaulting to now). ``email`` defaults to ``sub`` since
        the single-user allowlist keys on email.
        """
        iat = issued_at if issued_at is not None else _now()
        exp = iat + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None
        extra.setdefault("email", sub)
        return cls(
            sub=sub,
            aud=aud,
            iss=iss,
            iat=iat,
            exp=exp,
            scope=scope,
            client_id=client_id,
            **extra,
        )

    def is_expired(self, *, now: int | None = None) -> bool:
        """Return ``True`` when ``exp`` is set and in the past."""
        if self.exp is None:
            return False
        return (now if now is not None else _now()) >= self.exp

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable claim dict, omitting ``None`` values."""
        return self.model_dump(exclude_none=True)


class AuthorizationCode(BaseModel):
    """A short-lived OAuth authorization code awaiting exchange for a token."""

    model_config = ConfigDict(extra="ignore")

    code: SecretStr = Field(description="Opaque authorization code value.")
    client_id: str = Field(description="Client the code was issued to.")
    redirect_uri: str = Field(description="Redirect URI bound to the code.")
    user_email: str = Field(description="Authenticated user (email).")
    scope: str = Field(default=DEFAULT_SCOPE, description="Requested scope.")
    code_challenge: str | None = Field(
        default=None, description="PKCE code challenge."
    )
    code_challenge_method: str | None = Field(
        default=None, description="PKCE method (e.g. S256)."
    )
    issued_at: int = Field(default_factory=_now, description="Issued-at (POSIX).")
    expires_at: int | None = Field(default=None, description="Expiry (POSIX).")

    def is_expired(self, *, now: int | None = None) -> bool:
        """Return ``True`` when ``expires_at`` is set and in the past."""
        if self.expires_at is None:
            return False
        return (now if now is not None else _now()) >= self.expires_at


class AccessToken(BaseModel):
    """An issued OAuth access (bearer) token and its decoded claims."""

    model_config = ConfigDict(extra="ignore")

    token: SecretStr = Field(description="Encoded bearer token value.")
    claims: TokenClaims = Field(description="Decoded claims for the token.")
    token_type: str = Field(default="Bearer", description="OAuth token type.")
    expires_at: int | None = Field(default=None, description="Expiry (POSIX).")
    scope: str = Field(default=DEFAULT_SCOPE, description="Granted scope.")

    def is_expired(self, *, now: int | None = None) -> bool:
        """Return ``True`` when ``expires_at`` is set and in the past."""
        if self.expires_at is None:
            return self.claims.is_expired(now=now)
        return (now if now is not None else _now()) >= self.expires_at


class RefreshToken(BaseModel):
    """An issued OAuth refresh token bound to a client and user."""

    model_config = ConfigDict(extra="ignore")

    token: SecretStr = Field(description="Opaque refresh token value.")
    client_id: str = Field(description="Client the token was issued to.")
    user_email: str = Field(description="Authenticated user (email).")
    scope: str = Field(default=DEFAULT_SCOPE, description="Granted scope.")
    issued_at: int = Field(default_factory=_now, description="Issued-at (POSIX).")
    expires_at: int | None = Field(default=None, description="Expiry (POSIX).")

    def is_expired(self, *, now: int | None = None) -> bool:
        """Return ``True`` when ``expires_at`` is set and in the past."""
        if self.expires_at is None:
            return False
        return (now if now is not None else _now()) >= self.expires_at


class RegisteredClient(BaseModel):
    """An OAuth client registered with the MCP server.

    Supports dynamic client registration (RFC 7591). Public clients (e.g. the
    Claude Cowork PKCE flow) carry no secret; confidential clients store one as
    a :class:`~pydantic.SecretStr`.
    """

    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(description="Unique client identifier.")
    client_secret: SecretStr | None = Field(
        default=None, description="Confidential client secret, if any."
    )
    redirect_uris: list[str] = Field(
        default_factory=list, description="Allowed redirect URIs."
    )
    client_name: str | None = Field(default=None, description="Human-readable name.")
    grant_types: list[str] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"],
        description="Permitted OAuth grant types.",
    )
    scope: str = Field(default=DEFAULT_SCOPE, description="Client default scope.")
    issued_at: int = Field(default_factory=_now, description="Registration time.")

    @property
    def is_public(self) -> bool:
        """Return ``True`` for a public client (no secret)."""
        return self.client_secret is None

    def allows_redirect(self, redirect_uri: str) -> bool:
        """Return whether ``redirect_uri`` is registered for this client."""
        return redirect_uri in self.redirect_uris


class AllowedUser(BaseModel):
    """A user permitted to authenticate (single-user allowlist entry)."""

    model_config = ConfigDict(extra="ignore")

    email: str = Field(description="Permitted email address.")

    @property
    def normalized_email(self) -> str:
        """Return the case-folded, stripped email for comparison."""
        return self.email.strip().casefold()


class OAuthError(BaseModel):
    """An OAuth 2.0 error response (RFC 6749 section 5.2 / RFC 6750)."""

    model_config = ConfigDict(extra="ignore")

    error: str = Field(description="Machine-readable OAuth error code.")
    error_description: str | None = Field(
        default=None, description="Human-readable explanation."
    )
    error_uri: str | None = Field(default=None, description="Error documentation URI.")
    state: str | None = Field(default=None, description="Echoed client state.")

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serializable error dict, omitting ``None`` values."""
        return self.model_dump(exclude_none=True)


# Common OAuth error codes for convenience at call sites.
class OAuthErrorCode:
    """Standard OAuth 2.0 / bearer-token error codes."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CLIENT = "invalid_client"
    INVALID_GRANT = "invalid_grant"
    UNAUTHORIZED_CLIENT = "unauthorized_client"
    UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
    INVALID_SCOPE = "invalid_scope"
    ACCESS_DENIED = "access_denied"
    INVALID_TOKEN = "invalid_token"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    SERVER_ERROR = "server_error"


__all__ = [
    "DEFAULT_SCOPE",
    "TokenClaims",
    "AuthorizationCode",
    "AccessToken",
    "RefreshToken",
    "RegisteredClient",
    "AllowedUser",
    "OAuthError",
    "OAuthErrorCode",
]
