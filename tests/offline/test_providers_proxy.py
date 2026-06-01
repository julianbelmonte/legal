"""Offline unit tests for the proxy provider layer.

Pure logic only — no network at import or in any helper. Credentials come from
env (``LEGAL_FLOXY_USER`` / ``LEGAL_FLOXY_PASS``) via the config secret chain,
and proxy selection from ``LEGAL_PROXY_*`` settings.
"""

from __future__ import annotations

import pytest

from legal.providers.proxy import (
    FloxyBackend,
    NullProxyBackend,
    detect_backend,
    get_backend,
    list_backends,
    parse_proxy_for_playwright,
    parse_proxy_url,
    resolve_proxy,
    resolve_proxy_for_playwright,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_lists_floxy_and_none():
    names = list_backends()
    assert "floxy" in names
    assert "none" in names


def test_get_backend_returns_instances():
    assert isinstance(get_backend("floxy"), FloxyBackend)
    assert isinstance(get_backend("none"), NullProxyBackend)


def test_get_backend_unknown_raises_key_error():
    with pytest.raises(KeyError):
        get_backend("nope")


# ---------------------------------------------------------------------------
# FloxyBackend.build_proxy_url + matches_url
# ---------------------------------------------------------------------------


def test_floxy_build_proxy_url_formats_sticky_url(monkeypatch):
    monkeypatch.setenv("LEGAL_FLOXY_USER", "myuser")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "mypass")
    url = FloxyBackend().build_proxy_url("us", session="s1")
    assert url == (
        "http://myuser-package-mobile-country-US-session-s1-time-20"
        ":mypass@mobile.floxy.io:8080"
    )


def test_floxy_build_proxy_url_uppercases_country(monkeypatch):
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    url = FloxyBackend().build_proxy_url("de", session="abc")
    assert "country-DE" in url


def test_floxy_matches_url():
    backend = FloxyBackend()
    assert backend.matches_url("http://x@mobile.floxy.io:8080") is True
    assert backend.matches_url("http://x@example.com:8080") is False


def test_null_backend_build_raises_and_matches_false():
    backend = NullProxyBackend()
    assert backend.matches_url("http://anything") is False
    with pytest.raises(RuntimeError):
        backend.build_proxy_url("us")


def test_detect_backend_floxy():
    assert isinstance(
        detect_backend("http://u:p@mobile.floxy.io:8080"), FloxyBackend
    )
    assert detect_backend("http://u:p@example.com:8080") is None


# ---------------------------------------------------------------------------
# parse helpers round-trip
# ---------------------------------------------------------------------------


def test_parse_proxy_url_splits_parts():
    user, password, server = parse_proxy_url("http://u:p@host.example:8080")
    assert user == "u"
    assert password == "p"
    assert server == "host.example:8080"


def test_parse_proxy_for_playwright_round_trip(monkeypatch):
    monkeypatch.setenv("LEGAL_FLOXY_USER", "fuser")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "fpass")
    url = FloxyBackend().build_proxy_url("us", session="zz")
    user, password, server = parse_proxy_url(url)
    pw = parse_proxy_for_playwright(url)
    assert pw["server"] == f"http://{server}"
    assert pw["username"] == user
    assert pw["password"] == password
    assert pw["server"] == "http://mobile.floxy.io:8080"


# ---------------------------------------------------------------------------
# resolve_proxy enabled / disabled (env-driven)
# ---------------------------------------------------------------------------


def test_resolve_proxy_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("LEGAL_PROXY_ENABLED", raising=False)
    from legal.settings import reload_settings

    reload_settings()
    assert resolve_proxy() is None
    assert resolve_proxy_for_playwright() is None


def test_resolve_proxy_provider_none_returns_none(monkeypatch):
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "none")
    from legal.settings import reload_settings

    reload_settings()
    assert resolve_proxy() is None


def test_resolve_proxy_enabled_floxy_builds_url(monkeypatch):
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    from legal.settings import reload_settings

    reload_settings()
    url = resolve_proxy(session="sess")
    assert url == (
        "http://u-package-mobile-country-US-session-sess-time-20"
        ":p@mobile.floxy.io:8080"
    )
    assert FloxyBackend().matches_url(url)


def test_resolve_proxy_for_playwright_enabled_returns_dict(monkeypatch):
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    from legal.settings import reload_settings

    reload_settings()
    pw = resolve_proxy_for_playwright(session="sess")
    assert pw is not None
    assert pw["server"] == "http://mobile.floxy.io:8080"
    assert pw["username"].startswith("u-package-mobile-country-US")
    assert pw["password"] == "p"
