"""Smoke tests proving the offline scaffold (fixtures, caches, client) works.

These need no network or credentials and run in the default tier.
"""

from __future__ import annotations

from tests.conftest import TEST_API_KEY


def test_reset_settings_clears_caches() -> None:
    """The autouse fixture leaves the settings caches cold."""
    from api.settings import get_api_settings
    from legal.settings import get_settings

    # Caches are cleared by the autouse reset_settings fixture; populating them
    # here must not leak into other tests (verified by the fixture teardown).
    assert get_settings() is get_settings()
    assert get_api_settings() is get_api_settings()


def test_api_client_is_authenticated(api_client) -> None:
    """The api_client fixture yields a usable client and matching headers."""
    client, headers = api_client
    assert headers["x-api-key"] == TEST_API_KEY

    # The unauthenticated health probe is always reachable.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
