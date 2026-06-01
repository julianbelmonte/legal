"""Shared pytest fixtures and configuration for the legal test suite.

Two tiers:

* **offline** (default) — fast, free, no network or credentials.
* **live** (gated) — collected always but skipped unless ``LEGAL_LIVE=1``.

This module also resets env-dependent settings caches between tests and
provides an authenticated ``TestClient`` fixture for the API.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

#: API key injected by the ``api_client`` fixture for authenticated requests.
TEST_API_KEY = "test-key-offline"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``live``-marked tests unless ``LEGAL_LIVE=1`` is set."""
    if os.environ.get("LEGAL_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live tests require LEGAL_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def reset_settings() -> Iterator[None]:
    """Clear env-dependent settings caches before and after each test.

    Tests monkeypatch environment variables; the ``lru_cache`` on the settings
    accessors would otherwise leak stale configuration between tests.
    """

    def _clear() -> None:
        from api.settings import get_api_settings
        from legal.settings import get_settings

        get_settings.cache_clear()
        get_api_settings.cache_clear()

    _clear()
    try:
        yield
    finally:
        _clear()


@pytest.fixture()
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[object, dict[str, str]]]:
    """Yield a configured ``TestClient`` and the auth header dict.

    Sets ``LEGAL_API_KEY`` to a known test key, resets the api-settings cache so
    the key takes effect, and returns ``(client, headers)`` where ``headers``
    carries the matching ``x-api-key``.
    """
    from fastapi.testclient import TestClient

    from api.main import create_app
    from api.settings import get_api_settings

    monkeypatch.setenv("LEGAL_API_KEY", TEST_API_KEY)
    get_api_settings.cache_clear()

    app = create_app()
    headers = {"x-api-key": TEST_API_KEY}
    with TestClient(app) as client:
        yield client, headers


@pytest.fixture()
def live_secrets() -> dict[str, str]:
    """Return the secrets the live tier needs, or skip if any are missing.

    Only meaningful under ``LEGAL_LIVE=1``; offline runs never reach live tests.
    Resolves the Capsolver key and Floxy credentials through the pipeline's
    normal resolution chain (env -> ``legal.secret`` -> ``legal.local_config``)
    and skips with a clear message when one is not configured.
    """
    from legal import config

    secrets: dict[str, str] = {}
    for name, getter in (
        ("CAPSOLVER_API_KEY", config.capsolver_api_key),
        ("FLOXY_USER", config.floxy_user),
        ("FLOXY_PASS", config.floxy_pass),
    ):
        try:
            secrets[name] = getter()
        except RuntimeError:
            pytest.skip(f"live secret {name} is not configured")
    return secrets
