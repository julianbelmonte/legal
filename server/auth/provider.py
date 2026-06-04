"""Single-user OAuth provider for the MCP server.

The remote MCP endpoint is public HTTPS but only one configured identity may
authorize a client (e.g. Claude Cowork). This module implements that policy:

- an authorization-code flow with PKCE (RFC 7636, ``S256``) so public clients
  can authorize without a client secret;
- a minimal single-user login/consent check gated by
  ``LEGAL_MCP_OAUTH_LOGIN_SECRET`` and the ``LEGAL_MCP_ALLOWED_EMAILS``
  allowlist;
- signed JWT access tokens (pyjwt, ``HS256``) scoped to the MCP resource
  (``aud = settings.resource()``, ``iss = settings.issuer()``);
- bearer-token verification that validates signature/audience/issuer/expiry and
  re-checks the allowlist, returning a :class:`~server.auth.models.TokenClaims`.

Security notes:

- The single-user credential is **only** ``LEGAL_MCP_OAUTH_LOGIN_SECRET``. The
  legal API key (``LEGAL_API_KEY``) is never accepted as a login credential.
- Login secret and PKCE checks use constant-time comparison.
- Tokens are rejected when the subject is not (or is no longer) on the
  allowlist, so revoking an email invalidates outstanding tokens.

The concrete class is intentionally importable without network access and aligns
loosely with the ``mcp.server.auth`` SDK token-verifier shape (an async
``verify_token`` is provided alongside the synchronous helpers used in tests).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

import jwt
from pydantic import SecretStr

from server.auth.models import (
    DEFAULT_SCOPE,
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    TokenClaims,
)
from server.settings import McpSettings, get_mcp_settings

# JWT signing algorithm for issued access tokens.
_JWT_ALG = "HS256"
# Lifetime, in seconds, of an unredeemed authorization code.
_AUTH_CODE_TTL_SECONDS = 600
# ``token_use`` claim values distinguishing the two issued JWT kinds. A refresh
# token must never be accepted as a bearer access token (and vice versa), so the
# value is asserted on each verification path.
_TOKEN_USE_ACCESS = "access"
_TOKEN_USE_REFRESH = "refresh"
# Default refresh-token lifetime when settings do not specify one (90 days).
_DEFAULT_REFRESH_TTL_SECONDS = 7776000


def _now() -> int:
    """Return the current POSIX time as an integer (seconds)."""
    return int(time.time())


def _b64url_no_pad(raw: bytes) -> str:
    """Return base64url encoding of ``raw`` without ``=`` padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def compute_s256_challenge(code_verifier: str) -> str:
    """Return the PKCE ``S256`` challenge for ``code_verifier`` (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


class OAuthProviderError(Exception):
    """Raised when an OAuth operation fails (login, exchange, verification)."""

    def __init__(self, error: str, description: str | None = None) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description


@dataclass
class SingleUserOAuthProvider:
    """OAuth provider that authorizes only configured single-user identities.

    Build one with :meth:`for_settings`. The provider keeps an in-memory store
    of pending authorization codes keyed by their opaque value; codes are
    single-use and short-lived. Access tokens are stateless signed JWTs, so no
    token store is needed.
    """

    allowed_emails: frozenset[str]
    signing_key: str
    login_secret: str
    issuer: str
    resource: str
    token_ttl_seconds: int
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS
    redirect_uris: frozenset[str] = field(default_factory=frozenset)
    _codes: dict[str, AuthorizationCode] = field(default_factory=dict, repr=False)

    # --- construction --------------------------------------------------------

    @classmethod
    def for_settings(
        cls, settings: McpSettings | None = None
    ) -> "SingleUserOAuthProvider":
        """Build the provider from MCP settings (defaults to ``get_mcp_settings``)."""
        settings = settings or get_mcp_settings()
        return cls(
            allowed_emails=frozenset(settings.allowed_email_set()),
            signing_key=settings.oauth_signing_key.get_secret_value(),
            login_secret=settings.oauth_login_secret.get_secret_value(),
            issuer=settings.issuer(),
            resource=settings.resource(),
            token_ttl_seconds=settings.oauth_token_ttl_seconds,
            refresh_ttl_seconds=getattr(
                settings, "oauth_refresh_ttl_seconds", _DEFAULT_REFRESH_TTL_SECONDS
            ),
            redirect_uris=frozenset(settings.allowed_redirect_uris()),
        )

    # --- allowlist / login ---------------------------------------------------

    def is_allowed_user(self, email: str) -> bool:
        """Return ``True`` only for emails on the configured allowlist."""
        if not email:
            return False
        return email.strip().casefold() in self.allowed_emails

    def verify_login(self, email: str, secret: str) -> bool:
        """Return ``True`` when ``email`` is allowed and ``secret`` matches.

        The single-user credential is ``LEGAL_MCP_OAUTH_LOGIN_SECRET`` only;
        the legal API key is never accepted here. Both checks must pass, and an
        unconfigured login secret rejects every attempt.
        """
        if not self.is_allowed_user(email):
            return False
        if not self.login_secret:
            return False
        return hmac.compare_digest(secret or "", self.login_secret)

    # --- authorization-code flow (PKCE) --------------------------------------

    def create_authorization_code(
        self,
        *,
        email: str,
        secret: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str | None = None,
        code_challenge_method: str | None = "S256",
        scope: str = DEFAULT_SCOPE,
        now: int | None = None,
    ) -> AuthorizationCode:
        """Authenticate the user and mint a short-lived authorization code.

        Rejects users outside the allowlist, a wrong login secret, redirect URIs
        outside the configured allowlist (when one is set), and unsupported PKCE
        methods. Public clients without PKCE are permitted, but when a challenge
        is supplied only ``S256`` is accepted.
        """
        if not self.verify_login(email, secret):
            raise OAuthProviderError(
                "access_denied", "user not allowed or login secret invalid"
            )
        if self.redirect_uris and redirect_uri not in self.redirect_uris:
            raise OAuthProviderError(
                "invalid_request", "redirect_uri is not allowlisted"
            )
        if code_challenge is not None and (code_challenge_method or "S256") != "S256":
            raise OAuthProviderError(
                "invalid_request", "only the S256 PKCE method is supported"
            )

        issued_at = now if now is not None else _now()
        value = secrets.token_urlsafe(32)
        record = AuthorizationCode(
            code=SecretStr(value),
            client_id=client_id,
            redirect_uri=redirect_uri,
            user_email=email.strip(),
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method if code_challenge else None,
            issued_at=issued_at,
            expires_at=issued_at + _AUTH_CODE_TTL_SECONDS,
        )
        self._codes[value] = record
        return record

    def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        now: int | None = None,
    ) -> AccessToken:
        """Exchange an authorization code (+ PKCE verifier) for an access token.

        Validates the code binding (client id, redirect uri), expiry, single-use
        semantics, and — when the code carried a challenge — the PKCE ``S256``
        verifier. On success the code is consumed and a signed access token for
        the bound user is returned.
        """
        now = now if now is not None else _now()
        record = self._codes.get(code)
        if record is None:
            raise OAuthProviderError("invalid_grant", "unknown authorization code")
        # Single-use: consume regardless of subsequent validation outcome.
        self._codes.pop(code, None)

        if record.is_expired(now=now):
            raise OAuthProviderError("invalid_grant", "authorization code expired")
        if record.client_id != client_id:
            raise OAuthProviderError("invalid_grant", "client_id mismatch")
        if record.redirect_uri != redirect_uri:
            raise OAuthProviderError("invalid_grant", "redirect_uri mismatch")

        if record.code_challenge is not None:
            if not code_verifier:
                raise OAuthProviderError(
                    "invalid_grant", "code_verifier required for PKCE"
                )
            expected = record.code_challenge
            actual = compute_s256_challenge(code_verifier)
            if not hmac.compare_digest(actual, expected):
                raise OAuthProviderError(
                    "invalid_grant", "PKCE code_verifier does not match challenge"
                )

        # Belt-and-suspenders: the bound user must still be on the allowlist.
        if not self.is_allowed_user(record.user_email):
            raise OAuthProviderError("access_denied", "user not allowed")

        return self.issue_access_token(
            email=record.user_email,
            client_id=record.client_id,
            scope=record.scope,
            now=now,
        )

    # --- token issuance / verification ---------------------------------------

    def issue_access_token(
        self,
        *,
        email: str,
        client_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        now: int | None = None,
    ) -> AccessToken:
        """Issue a signed JWT access token scoped to the MCP resource."""
        if not self.is_allowed_user(email):
            raise OAuthProviderError("access_denied", "user not allowed")
        if not self.signing_key:
            raise OAuthProviderError(
                "server_error", "OAuth signing key is not configured"
            )
        claims = TokenClaims.build(
            sub=email.strip(),
            aud=self.resource,
            iss=self.issuer,
            ttl_seconds=self.token_ttl_seconds,
            scope=scope,
            client_id=client_id,
            issued_at=now,
            jti=secrets.token_urlsafe(12),
            token_use=_TOKEN_USE_ACCESS,
        )
        token = jwt.encode(claims.to_payload(), self.signing_key, algorithm=_JWT_ALG)
        return AccessToken(
            token=SecretStr(token),
            claims=claims,
            expires_at=claims.exp,
            scope=scope,
        )

    def issue_refresh_token(
        self,
        *,
        email: str,
        client_id: str | None = None,
        scope: str = DEFAULT_SCOPE,
        now: int | None = None,
    ) -> RefreshToken:
        """Issue a long-lived, stateless signed-JWT refresh token.

        The refresh token is a JWT signed with the same key as access tokens but
        carrying ``token_use=refresh`` so it cannot be presented as a bearer
        (``decode_token`` rejects it). It is stateless, so it survives app
        restarts/redeploys as long as the signing key is stable.
        """
        if not self.is_allowed_user(email):
            raise OAuthProviderError("access_denied", "user not allowed")
        if not self.signing_key:
            raise OAuthProviderError(
                "server_error", "OAuth signing key is not configured"
            )
        issued_at = now if now is not None else _now()
        claims = TokenClaims.build(
            sub=email.strip(),
            aud=self.resource,
            iss=self.issuer,
            ttl_seconds=self.refresh_ttl_seconds,
            scope=scope,
            client_id=client_id,
            issued_at=issued_at,
            jti=secrets.token_urlsafe(12),
            token_use=_TOKEN_USE_REFRESH,
        )
        token = jwt.encode(claims.to_payload(), self.signing_key, algorithm=_JWT_ALG)
        return RefreshToken(
            token=SecretStr(token),
            client_id=client_id or "",
            user_email=email.strip(),
            scope=scope,
            issued_at=issued_at,
            expires_at=claims.exp,
        )

    def redeem_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str | None = None,
        now: int | None = None,
    ) -> tuple[AccessToken, RefreshToken]:
        """Exchange a valid refresh token for a fresh access + refresh token pair.

        Validates the refresh JWT (signature, audience, issuer, expiry, and the
        ``token_use=refresh`` marker), re-checks the bound subject against the
        allowlist, and — when the refresh token carries a ``client_id`` and the
        request supplies one — that they match. On success a new access token and
        a freshly minted refresh token are returned (sliding expiration), so an
        actively used connector renews indefinitely without re-login.
        """
        now = now if now is not None else _now()
        claims = self._decode_jwt(refresh_token, expected_use=_TOKEN_USE_REFRESH)
        if not self.is_allowed_user(claims.sub):
            raise OAuthProviderError("invalid_grant", "user not allowed")
        token_client = claims.client_id
        if client_id and token_client and client_id != token_client:
            raise OAuthProviderError("invalid_grant", "client_id mismatch")
        effective_client = client_id or token_client
        access = self.issue_access_token(
            email=claims.sub, client_id=effective_client, scope=claims.scope, now=now
        )
        refresh = self.issue_refresh_token(
            email=claims.sub, client_id=effective_client, scope=claims.scope, now=now
        )
        return access, refresh

    def _decode_jwt(self, token: str, *, expected_use: str) -> TokenClaims:
        """Decode + validate a signed token JWT and assert its ``token_use``.

        Verifies signature, audience (the MCP resource), issuer, and expiry, then
        enforces that the token's ``token_use`` matches ``expected_use`` so an
        access token cannot be redeemed as a refresh token and a refresh token
        cannot be presented as a bearer. The ``invalid_token`` error code is used
        for the bearer path and ``invalid_grant`` for the refresh path.
        """
        error_code = (
            "invalid_token" if expected_use == _TOKEN_USE_ACCESS else "invalid_grant"
        )
        try:
            payload = jwt.decode(
                token,
                self.signing_key,
                algorithms=[_JWT_ALG],
                audience=self.resource,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OAuthProviderError(error_code, str(exc)) from exc

        # Tokens minted before ``token_use`` existed are treated as access tokens
        # (backward compatible); refresh tokens always carry the explicit marker.
        token_use = payload.get("token_use", _TOKEN_USE_ACCESS)
        if token_use != expected_use:
            raise OAuthProviderError(
                error_code, f"expected a {expected_use} token, got {token_use}"
            )
        return TokenClaims.model_validate(payload)

    def decode_token(self, token: str) -> TokenClaims:
        """Validate a bearer access token and return its claims.

        Verifies signature, audience (the MCP resource), issuer, and expiry,
        rejects refresh tokens presented as bearers, then re-checks the subject
        against the allowlist. Raises :class:`OAuthProviderError`
        (``invalid_token``) on any failure.
        """
        claims = self._decode_jwt(token, expected_use=_TOKEN_USE_ACCESS)
        if not self.is_allowed_user(claims.sub):
            raise OAuthProviderError(
                "invalid_token", "token subject is not an allowed user"
            )
        return claims

    def verify_token_sync(self, token: str) -> TokenClaims | None:
        """Return claims for a valid token, or ``None`` when invalid."""
        try:
            return self.decode_token(token)
        except OAuthProviderError:
            return None

    async def verify_token(self, token: str) -> TokenClaims | None:
        """Async token verifier (aligns with the ``mcp`` SDK ``TokenVerifier``)."""
        return self.verify_token_sync(token)


__all__ = [
    "SingleUserOAuthProvider",
    "OAuthProviderError",
    "compute_s256_challenge",
]
