"""Offline tests for ``run_with_botbrowser`` proxy-exit rotation.

A browser-backed search that retries inside one context reuses one poisoned
proxy exit. ``run_with_botbrowser`` instead relaunches per attempt, so each retry
runs behind a fresh exit. These tests use a fake launcher (no real Chromium) to
pin: success on the first healthy exit, recovery after soft failures, launch
errors treated as rotate-and-retry, and a transparent ``BrowserExhausted``
carrying ``exits_tried`` when every exit fails.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from legal.browser import (
    BrowserExhausted,
    BrowserRetry,
    browser_unavailable_error,
    run_with_botbrowser,
)


class _FakePage:
    pass


class _FakeBrowser:
    def __init__(self, launches: list[int]) -> None:
        self._launches = launches
        self.page = _FakePage()

    def __enter__(self):
        self._launches.append(1)
        return self

    def __exit__(self, *exc):
        return False


def _launcher(launches: list[int]):
    return lambda hidden: _FakeBrowser(launches)


def test_returns_first_success_without_extra_launches():
    launches: list[int] = []
    result = run_with_botbrowser(
        lambda page, i: ("ok", i),
        retries=3,
        launcher=_launcher(launches),
    )
    assert result == ("ok", 1)
    assert len(launches) == 1


def test_recovers_after_soft_failures():
    launches: list[int] = []
    calls = {"n": 0}

    def attempt(page, index):
        calls["n"] += 1
        if index < 3:
            raise BrowserRetry({"error": "recaptcha rejected"})
        return ("done", index)

    result = run_with_botbrowser(attempt, retries=5, launcher=_launcher(launches))
    assert result == ("done", 3)
    assert len(launches) == 3  # one fresh exit per attempt


def test_launch_error_is_rotated():
    launches: list[int] = []

    class _ExplodingBrowser(_FakeBrowser):
        def __enter__(self):
            super().__enter__()
            raise PlaywrightError("net::ERR_TIMED_OUT through proxy")

    seq = [
        lambda hidden: _ExplodingBrowser(launches),
        lambda hidden: _FakeBrowser(launches),
    ]

    def launcher(hidden):
        return seq[len(launches)](hidden)

    result = run_with_botbrowser(lambda page, i: ("ok", i), retries=3, launcher=launcher)
    assert result == ("ok", 2)
    assert len(launches) == 2


def test_exhaustion_is_transparent():
    launches: list[int] = []

    def always_fail(page, index):
        raise BrowserRetry({"error": "search navigation did not complete"})

    with pytest.raises(BrowserExhausted) as excinfo:
        run_with_botbrowser(always_fail, retries=3, launcher=_launcher(launches))

    meta = excinfo.value.meta
    assert meta["exits_tried"] == 3
    assert meta["error"] == "search navigation did not complete"
    assert len(launches) == 3

    err = browser_unavailable_error(meta, source="csjn", operation="fallos")
    assert err.retryable is True
    assert "3 proxy exit" in err.message
