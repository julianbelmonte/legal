"""Deploy-time configuration for the legal pipeline.

A single, env-driven settings object that controls proxy and captcha provider
selection and related deploy toggles. Env prefix is ``LEGAL_``.

This module holds **non-secret** selection flags only. Secrets (API keys and
credentials) live in ``legal/secret.py`` / the environment and are resolved
elsewhere. Keep this module import-light: no network or heavy imports at import
time.
"""

from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-secret deploy configuration, read from the environment.

    All fields have sensible defaults so the offline tier needs no env.
    """

    model_config = SettingsConfigDict(env_prefix="LEGAL_", extra="ignore")

    proxy_enabled: bool = False
    proxy_provider: str = "none"  # "none" | "floxy" | "anyip"
    proxy_country: str = "us"
    # AnyIP exit pool: "mobile" (CGNAT, heavily WAF/reCAPTCHA-flagged),
    # "residential" (real-ISP IPs, best reCAPTCHA reputation), or "datacenter".
    anyip_type: str = "mobile"
    # When egress goes through a proxy, individual residential/mobile exits are
    # flaky: a dead exit hangs to the timeout wall and retrying the *same* exit
    # cannot recover. With rotation on, the HTTP client and browser launcher
    # abandon a failed exit and retry behind a fresh proxy session.
    proxy_rotate_on_failure: bool = True
    captcha_provider: str = "capsolver"
    # CSJN reCAPTCHA Enterprise handling: "native" runs the page's own scoring
    # (the only config with demonstrated end-to-end success so far), or
    # "capsolver" injects a provider token (wired and occasionally accepted, but
    # not yet reliably better than native — kept as an opt-in for further tuning).
    csjn_captcha: str = "native"
    botbrowser_profile: str | None = None  # pin a specific .enc by name


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the settings cache and return a freshly-loaded instance.

    Tests use this to apply monkeypatched environment variables.
    """
    get_settings.cache_clear()
    return get_settings()
