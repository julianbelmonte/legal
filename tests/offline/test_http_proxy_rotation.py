"""Offline tests for HTTP-client proxy-exit rotation.

A flaky residential/mobile proxy exit stalls and a same-exit retry cannot
recover. ``LegalHttpClient`` therefore rebuilds behind a fresh exit between
retries when the proxy is active. These tests pin: the rotation gate
(``proxy_active`` + setting), that an injected transport/pinned proxy is never
rotated, and that a transient failure rotates and then succeeds while carrying
cookies forward.
"""

from __future__ import annotations

import httpx
import pytest

import legal.http as http_mod
from legal.http import HttpSettings, LegalHttpClient


@pytest.fixture
def proxy_on(monkeypatch):
    monkeypatch.setattr(http_mod, "_rotation_enabled", lambda: True)
    yield


class _FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeClient:
    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.cookies = httpx.Cookies()
        self.closed = False

    def request(self, method, url, **kwargs):
        return self._behavior()

    def close(self) -> None:
        self.closed = True


def test_rotatable_only_for_auto_resolved_proxy(proxy_on, monkeypatch):
    monkeypatch.setattr(http_mod, "build_client", lambda *a, **k: _FakeClient(lambda: _FakeResp()))
    auto = LegalHttpClient()
    assert auto._rotatable is True

    # An injected transport (offline MockTransport) must never be rotated.
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    injected = LegalHttpClient(transport=transport)
    assert injected._rotatable is False

    # An explicitly pinned proxy is the caller's choice; leave it.
    pinned = LegalHttpClient(proxy="http://user:pass@host:1080")
    assert pinned._rotatable is False


def test_rotation_disabled_when_proxy_inactive(monkeypatch):
    monkeypatch.setattr(http_mod, "_rotation_enabled", lambda: False)
    monkeypatch.setattr(http_mod, "build_client", lambda *a, **k: _FakeClient(lambda: _FakeResp()))
    client = LegalHttpClient()
    assert client._rotatable is False


def test_transient_failure_rotates_then_succeeds(proxy_on, monkeypatch):
    built: list[_FakeClient] = []

    def boom():
        raise httpx.ConnectTimeout("exit stalled")

    def ok():
        return _FakeResp(200)

    behaviors = [boom, ok]

    def fake_build(*args, **kwargs):
        c = _FakeClient(behaviors[len(built)] if len(built) < len(behaviors) else ok)
        built.append(c)
        return c

    monkeypatch.setattr(http_mod, "build_client", fake_build)
    monkeypatch.setattr(LegalHttpClient, "_sleep_before_retry", lambda self, attempt: None)

    client = LegalHttpClient(settings=HttpSettings(retries=2))
    # Seed a cookie on the first (about-to-fail) exit; it must survive rotation.
    client._client.cookies.set("sid", "abc")

    resp = client.request("GET", "https://example/x")
    assert resp.status_code == 200
    assert len(built) == 2  # rotated to a fresh exit
    assert built[0].closed is True  # the dead exit was torn down
    assert client._client.cookies.get("sid") == "abc"  # cookies carried forward
