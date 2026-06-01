"""Offline tests for the pipeline seams.

These guard the "plug without interfering" requirement:

* HTTP egress passes a proxy only when configured (and never with a transport).
* The browser launcher passes a ``proxy`` kwarg + ``configs.proxy`` only when
  proxy is enabled — verified by inspecting ``launch_persistent_context`` kwargs
  WITHOUT launching a real browser.
* ``config.pick_profile`` rotates across ``.enc`` profiles and honors the
  ``LEGAL_BOTBROWSER_PROFILE`` pin.
* ``dispatch.run_operation`` threads params into the handler's ``Namespace`` and
  raises ``LegalCliError(usage_error)`` for an unknown source/op.
* ``settings.get_settings`` reflects env overrides after ``reload_settings``.

No network, no browser, no credentials.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest import mock

import httpx
import pytest

from legal import config
from legal import http as legal_http
from legal.dispatch import resolve_operation
from legal.errors import LegalCliError
from legal.settings import get_settings, reload_settings
from legal.sources.base import SourceOperation


# ---------------------------------------------------------------------------
# 1. http.build_client proxy injection
# ---------------------------------------------------------------------------


def _captured_client_kwargs() -> mock.MagicMock:
    """Patch ``httpx.Client`` and return the mock so we can read its kwargs."""
    return mock.patch.object(legal_http.httpx, "Client", autospec=True)


def test_build_client_default_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEGAL_PROXY_ENABLED", raising=False)
    reload_settings()
    with _captured_client_kwargs() as client_cls:
        legal_http.build_client()
    _, kwargs = client_cls.call_args
    assert "proxy" not in kwargs


def test_build_client_floxy_proxy_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    reload_settings()
    with _captured_client_kwargs() as client_cls:
        legal_http.build_client()
    _, kwargs = client_cls.call_args
    assert "proxy" in kwargs
    proxy = kwargs["proxy"]
    assert isinstance(proxy, str)
    assert "floxy.io" in proxy


def test_build_client_transport_wins_over_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with proxy enabled, an explicit transport must suppress the proxy.
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    reload_settings()
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with _captured_client_kwargs() as client_cls:
        legal_http.build_client(transport=transport)
    _, kwargs = client_cls.call_args
    assert "proxy" not in kwargs
    assert kwargs["transport"] is transport


# ---------------------------------------------------------------------------
# 2. browser.py proxy kwarg + configs.proxy (no real browser)
# ---------------------------------------------------------------------------


def _make_browser_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Wire up a BotBrowser test harness that never launches Chromium.

    Returns the ``launch_persistent_context`` mock so the test can inspect the
    kwargs the launcher built.
    """
    import legal.browser as browser

    # A single fake .enc profile so config.pick_profile() resolves offline.
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "p0.enc").write_text(json.dumps({"fingerprint": {"id": 0}}))
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILES_DIR", str(profiles_dir))
    monkeypatch.setenv("LEGAL_BOTBROWSER_BIN", str(tmp_path / "fake-chromium"))
    (tmp_path / "fake-chromium").write_text("#!/bin/sh\n")
    reload_settings()

    launch_ctx = mock.MagicMock(name="launch_persistent_context")
    fake_page = mock.MagicMock(name="page")
    launch_ctx.return_value.pages = [fake_page]

    fake_pw = mock.MagicMock(name="playwright")
    fake_pw.chromium.launch_persistent_context = launch_ctx

    sync_pw = mock.MagicMock(name="sync_playwright")
    sync_pw.return_value.start.return_value = fake_pw
    monkeypatch.setattr(browser, "sync_playwright", sync_pw)

    return launch_ctx


def test_browser_no_proxy_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import legal.browser as browser

    monkeypatch.delenv("LEGAL_PROXY_ENABLED", raising=False)
    launch_ctx = _make_browser_env(tmp_path, monkeypatch)
    reload_settings()

    with browser.BotBrowser():
        pass

    _, kwargs = launch_ctx.call_args
    assert "proxy" not in kwargs
    # And the merged BotBrowser profile carries no proxy in its configs.
    profile_arg = next(a for a in kwargs["args"] if a.startswith("--bot-profile="))
    merged = json.loads(Path(profile_arg.split("=", 1)[1]).read_text())
    assert "proxy" not in merged.get("configs", {})


def test_browser_proxy_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import legal.browser as browser

    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "us")
    monkeypatch.setenv("LEGAL_FLOXY_USER", "u")
    monkeypatch.setenv("LEGAL_FLOXY_PASS", "p")
    launch_ctx = _make_browser_env(tmp_path, monkeypatch)
    reload_settings()

    with browser.BotBrowser():
        pass

    _, kwargs = launch_ctx.call_args
    assert "proxy" in kwargs
    proxy = kwargs["proxy"]
    assert isinstance(proxy, dict)
    assert "floxy.io" in proxy["server"]
    # The proxy is also persisted into the merged profile's configs.
    profile_arg = next(a for a in kwargs["args"] if a.startswith("--bot-profile="))
    merged = json.loads(Path(profile_arg.split("=", 1)[1]).read_text())
    assert merged["configs"]["proxy"]["server"] == proxy["server"]


# ---------------------------------------------------------------------------
# 3. config.pick_profile rotation + pin
# ---------------------------------------------------------------------------


def _seed_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> Path:
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    for i in range(count):
        (profiles_dir / f"profile-{i}.enc").write_text("{}")
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILES_DIR", str(profiles_dir))
    return profiles_dir


def test_pick_profile_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles_dir = _seed_profiles(tmp_path, monkeypatch, count=5)
    monkeypatch.delenv("LEGAL_BOTBROWSER_PROFILE", raising=False)
    reload_settings()

    seen = {config.pick_profile() for _ in range(60)}
    assert len(seen) > 1
    for path in seen:
        assert path.parent == profiles_dir
        assert path.suffix == ".enc"


def test_pick_profile_honors_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_profiles(tmp_path, monkeypatch, count=5)
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILE", "profile-3")
    reload_settings()

    # Pin by stem; every draw must return the same pinned profile.
    for _ in range(10):
        assert config.pick_profile().name == "profile-3.enc"

    # Pin by full name also works.
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILE", "profile-1.enc")
    reload_settings()
    assert config.pick_profile().name == "profile-1.enc"


def test_pick_profile_unknown_pin_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_profiles(tmp_path, monkeypatch, count=3)
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILE", "does-not-exist")
    reload_settings()
    with pytest.raises(RuntimeError, match="does-not-exist"):
        config.pick_profile()


# ---------------------------------------------------------------------------
# 4. dispatch.run_operation namespace building + unknown source/op
# ---------------------------------------------------------------------------


def test_run_operation_threads_params_into_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legal.dispatch as dispatch

    real_op = resolve_operation("infoleg", "search")
    captured: dict[str, argparse.Namespace] = {}

    def capturing_handler(ns: argparse.Namespace) -> dict[str, object]:
        captured["ns"] = ns
        return {"ok": True}

    spy_op = SourceOperation(
        name=real_op.name,
        handler=capturing_handler,
        help=real_op.help,
        add_arguments=real_op.add_arguments,
    )

    def fake_resolve(source_id: str, operation: str) -> SourceOperation:
        assert (source_id, operation) == ("infoleg", "search")
        return spy_op

    monkeypatch.setattr(dispatch, "resolve_operation", fake_resolve)

    result = dispatch.run_operation(
        "infoleg",
        "search",
        {"text": "habeas corpus", "limit": 7},
        raw=True,
    )

    assert result == {"ok": True}
    ns = captured["ns"]
    assert ns.text == "habeas corpus"
    assert ns.limit == 7
    assert ns.raw is True
    assert ns.source == "infoleg"
    assert ns.operation == "search"
    assert ns.source_operation is spy_op


def test_run_operation_unknown_source_raises_usage_error() -> None:
    from legal.dispatch import run_operation

    with pytest.raises(LegalCliError) as excinfo:
        run_operation("no-such-source", "search", {})
    assert excinfo.value.code == "usage_error"


def test_run_operation_unknown_operation_raises_usage_error() -> None:
    from legal.dispatch import run_operation

    with pytest.raises(LegalCliError) as excinfo:
        run_operation("infoleg", "no-such-op", {})
    assert excinfo.value.code == "usage_error"


# ---------------------------------------------------------------------------
# 5. settings env overrides after reload_settings
# ---------------------------------------------------------------------------


def test_get_settings_reflects_env_after_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEGAL_PROXY_ENABLED", raising=False)
    monkeypatch.delenv("LEGAL_PROXY_PROVIDER", raising=False)
    base = reload_settings()
    assert base.proxy_enabled is False
    assert base.proxy_provider == "none"

    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "de")
    monkeypatch.setenv("LEGAL_CAPTCHA_PROVIDER", "capsolver")
    monkeypatch.setenv("LEGAL_BOTBROWSER_PROFILE", "pinned-one")

    updated = reload_settings()
    assert updated.proxy_enabled is True
    assert updated.proxy_provider == "floxy"
    assert updated.proxy_country == "de"
    assert updated.captcha_provider == "capsolver"
    assert updated.botbrowser_profile == "pinned-one"
    # The cached accessor returns the reloaded instance.
    assert get_settings() is updated
