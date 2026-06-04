"""Settings for the MCP server consumer.

Deploy-time configuration for the remote MCP server, read from the environment
with the ``LEGAL_MCP_`` prefix. This module is import-light (no network or heavy
imports) and follows the repo's established pydantic-settings pattern (see
``api/settings.py`` and ``legal/settings.py``).

Covers MCP runtime (public URL), OAuth (issuer, signing/login secrets, client
allowlist), single-user allowlisting (allowed emails), and document text cache
behavior (TTL, max page size). Secret fields use :class:`~pydantic.SecretStr`
so their values are masked in ``repr``/log output.

The server **fails closed**: when MCP auth is enabled (the default), the OAuth
signing key and login secret must be configured, or :meth:`McpSettings.validate_ready`
raises. With ``LEGAL_MCP_AUTH_ENABLED=false`` (intended for local offline
tests) the secrets may be absent.
"""

from __future__ import annotations

import functools

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    """MCP server configuration, read from the environment.

    All fields have defaults appropriate for local offline tests so the package
    is importable and the server constructable without secrets. Production
    deployments set the ``LEGAL_MCP_*`` env vars and keep ``auth_enabled`` true.
    """

    model_config = SettingsConfigDict(env_prefix="LEGAL_MCP_", extra="ignore")

    # --- MCP runtime ---------------------------------------------------------
    # Public HTTPS URL the MCP transport is reachable at (also the OAuth
    # resource/audience). Defaults to a local loopback URL for offline tests.
    public_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8080/mcp")

    # --- OAuth ---------------------------------------------------------------
    auth_enabled: bool = True
    # OAuth issuer. Empty means "derive from public_url" at use time.
    oauth_issuer: str = ""
    # Comma-separated allowlist of email addresses permitted to authenticate.
    allowed_emails: str = ""
    # Secret used to sign issued OAuth tokens. Required when auth is enabled.
    oauth_signing_key: SecretStr = SecretStr("")
    # Secret gating the single-user login form. Required when auth is enabled.
    oauth_login_secret: SecretStr = SecretStr("")
    # Comma-separated allowlist of OAuth client ids / redirect origins. Empty
    # means dynamic client registration is open (suitable for offline tests).
    oauth_client_allowlist: str = ""
    # Comma-separated allowlist of redirect URIs accepted in the authorization
    # flow. Empty means any redirect registered by the client is accepted.
    oauth_redirect_uris: str = ""
    # Lifetime, in seconds, of issued OAuth access tokens.
    oauth_token_ttl_seconds: int = 3600
    # Lifetime, in seconds, of issued OAuth refresh tokens. The client exchanges
    # a refresh token for a fresh access token before this elapses; each refresh
    # also re-issues the refresh token (sliding window), so an actively used
    # connector never needs to re-authenticate. Defaults to 90 days.
    oauth_refresh_ttl_seconds: int = 7776000

    # --- Document text cache -------------------------------------------------
    # Time-to-live, in seconds, for extracted document text cache records.
    cache_ttl_seconds: int = 3600
    # Maximum characters returned per document text page.
    max_page_size: int = 20000

    @field_validator(
        "cache_ttl_seconds",
        "max_page_size",
        "oauth_token_ttl_seconds",
        "oauth_refresh_ttl_seconds",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        return value

    def issuer(self) -> str:
        """Return the OAuth issuer, defaulting to the public URL."""
        return self.oauth_issuer or str(self.public_url)

    def resource(self) -> str:
        """Return the OAuth protected-resource URL / token audience."""
        return str(self.public_url)

    def allowed_redirect_uris(self) -> set[str]:
        """Return the configured redirect-URI allowlist; empty when none."""
        return {
            candidate.strip()
            for candidate in self.oauth_redirect_uris.split(",")
            if candidate.strip()
        }

    def allowed_email_set(self) -> set[str]:
        """Return the set of allowed (case-folded) emails; empty when none."""
        return {
            candidate.strip().casefold()
            for candidate in self.allowed_emails.split(",")
            if candidate.strip()
        }

    def allowed_client_set(self) -> set[str]:
        """Return the set of allowed OAuth client ids; empty when none."""
        return {
            candidate.strip()
            for candidate in self.oauth_client_allowlist.split(",")
            if candidate.strip()
        }

    def validate_ready(self) -> McpSettings:
        """Fail closed: ensure required OAuth secrets exist when auth is on.

        Returns ``self`` for chaining. Raises :class:`ValueError` when
        ``auth_enabled`` is true but the signing key or login secret is empty.
        """
        if not self.auth_enabled:
            return self
        missing: list[str] = []
        if not self.oauth_signing_key.get_secret_value():
            missing.append("LEGAL_MCP_OAUTH_SIGNING_KEY")
        if not self.oauth_login_secret.get_secret_value():
            missing.append("LEGAL_MCP_OAUTH_LOGIN_SECRET")
        if missing:
            raise ValueError(
                "MCP auth is enabled but required OAuth secrets are missing: "
                + ", ".join(missing)
                + " (set them, or disable auth with LEGAL_MCP_AUTH_ENABLED=false "
                "for local offline tests)"
            )
        return self


@functools.lru_cache(maxsize=1)
def get_mcp_settings() -> McpSettings:
    """Return the cached MCP settings instance."""
    return McpSettings()


def reload_mcp_settings() -> McpSettings:
    """Clear the settings cache and return a freshly-loaded instance.

    Tests use this to apply monkeypatched environment variables.
    """
    get_mcp_settings.cache_clear()
    return get_mcp_settings()


def load_settings() -> McpSettings:
    """Return MCP settings (uncached convenience alias for ``get_mcp_settings``)."""
    return get_mcp_settings()
