"""Deploy-time configuration for the FastAPI consumer.

Env-driven API settings, prefixed ``LEGAL_API_``. Holds the API key material
used to authenticate protected ``/v1`` routes. The API fails closed: with no
keys configured, :func:`allowed_keys` is empty and protected routes reject all
requests.

Keep this module import-light: no network or heavy imports at import time.
"""

from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """API configuration read from the environment.

    ``keys`` is a comma-separated list (``LEGAL_API_KEYS``); ``key`` is a
    single-key convenience (``LEGAL_API_KEY``). Both are merged by
    :meth:`allowed_keys`.
    """

    model_config = SettingsConfigDict(env_prefix="LEGAL_API_", extra="ignore")

    keys: str = ""
    key: str | None = None

    def allowed_keys(self) -> set[str]:
        """Return the set of accepted API keys (empty when none configured)."""
        result: set[str] = set()
        for raw in self.keys.split(","):
            candidate = raw.strip()
            if candidate:
                result.add(candidate)
        if self.key:
            candidate = self.key.strip()
            if candidate:
                result.add(candidate)
        return result


@functools.lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    """Return the cached API settings instance."""
    return ApiSettings()


def reload_api_settings() -> ApiSettings:
    """Clear the settings cache and return a freshly-loaded instance.

    Tests use this to apply monkeypatched environment variables.
    """
    get_api_settings.cache_clear()
    return get_api_settings()
