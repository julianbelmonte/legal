"""Fixtures for the live test tier.

Live tests are collected always but only run under ``LEGAL_LIVE=1`` (root
``conftest`` gating). Within the live run, individual tests that need
credentials skip cleanly when those credentials are absent so a partial-secrets
environment still runs what it can. Secrets resolve through the pipeline's
normal chain (env -> ``legal.secret`` -> ``legal.local_config``); the config
getters raise ``RuntimeError`` when a value is missing.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def requires_capsolver() -> str:
    """Skip unless a Capsolver API key is configured; return the key."""
    from legal import config

    try:
        return config.capsolver_api_key()
    except RuntimeError as exc:
        pytest.skip(f"Capsolver credentials not configured: {exc}")


@pytest.fixture()
def requires_floxy() -> tuple[str, str]:
    """Skip unless Floxy proxy credentials are configured; return (user, pass)."""
    from legal import config

    try:
        user = config.floxy_user()
        password = config.floxy_pass()
    except RuntimeError as exc:
        pytest.skip(f"Floxy credentials not configured: {exc}")
    return user, password
