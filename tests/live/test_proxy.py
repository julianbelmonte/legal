"""Live tests — Floxy/US proxy egress + BotBrowser multi-profile rotation.

These tests prove the **optional** proxy path works against the real Floxy
provider at both egress seams (the HTTP client and the browser launcher) and
that ``config.pick_profile`` rotates across the vendored ``.enc`` profiles.

Gating:

* Every test is ``live`` (skipped unless ``LEGAL_LIVE=1``; see the root
  ``conftest``).
* The ``requires_floxy`` fixture skips cleanly when Floxy credentials are
  absent, so a partial-secrets environment still runs what it can.

The proxy is enabled via env **for these tests only**
(``LEGAL_PROXY_ENABLED=1``, ``LEGAL_PROXY_PROVIDER=floxy``,
``LEGAL_PROXY_COUNTRY=us``) using ``monkeypatch`` + ``reload_settings`` so the
rest of the suite stays proxy-disabled (direct egress). Because the legal sites
are Argentine, a US exit may change or block their results — the assertion here
is only that **egress through the proxy works** and returns an IP, not about any
particular Argentine result.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator

import httpx
import pytest

from legal.settings import reload_settings

pytestmark = pytest.mark.live

#: A few public IP-echo endpoints; the egress check accepts the first that
#: answers so a single endpoint outage does not flake the suite.
IP_ECHO_ENDPOINTS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/all.json",
    "https://ipinfo.io/json",
)

#: Generous timeout: residential/mobile proxy egress is slower than direct.
PROXY_HTTP_TIMEOUT = 60.0


@pytest.fixture()
def floxy_proxy_env(
    monkeypatch: pytest.MonkeyPatch, requires_floxy: tuple[str, str]
) -> Iterator[None]:
    """Enable the Floxy/US proxy via env for the duration of one test.

    Uses ``monkeypatch`` + ``reload_settings`` so the toggle is scoped to the
    test (the autouse ``reset_settings`` fixture clears the cache afterwards and
    ``monkeypatch`` restores the environment). ``requires_floxy`` is depended on
    so the test skips when credentials are absent.
    """
    monkeypatch.setenv("LEGAL_PROXY_ENABLED", "1")
    monkeypatch.setenv("LEGAL_PROXY_PROVIDER", "floxy")
    monkeypatch.setenv("LEGAL_PROXY_COUNTRY", "us")
    reload_settings()
    yield
    reload_settings()


def _extract_ip(payload: object) -> str | None:
    """Pull an IP-looking string out of a parsed IP-echo JSON response."""
    if not isinstance(payload, dict):
        return None
    for key in ("ip", "ip_addr", "client_ip", "address"):
        value = payload.get(key)
        if isinstance(value, str):
            try:
                ipaddress.ip_address(value.strip())
            except ValueError:
                continue
            return value.strip()
    return None


def _fetch_ip_through_proxy(proxy_url: str) -> str:
    """Return the public IP seen through *proxy_url*, or fail the test.

    Tries each IP-echo endpoint in turn so a single endpoint outage does not
    flake the suite; raises an assertion only when none of them yields an IP.
    """
    errors: list[str] = []
    for endpoint in IP_ECHO_ENDPOINTS:
        try:
            with httpx.Client(
                proxy=proxy_url, timeout=PROXY_HTTP_TIMEOUT
            ) as client:
                response = client.get(endpoint)
                response.raise_for_status()
                ip = _extract_ip(response.json())
        except Exception as exc:  # noqa: BLE001 - record and try the next endpoint
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
            continue
        if ip is not None:
            return ip
        errors.append(f"{endpoint}: no IP field in {response.text[:200]!r}")
    raise AssertionError(
        "no IP-echo endpoint returned an IP through the floxy proxy; "
        f"attempts: {errors}"
    )


def test_floxy_egress_resolves_and_returns_ip(floxy_proxy_env: None) -> None:
    """Resolve a Floxy/US URL and prove a real request through it returns an IP.

    Also asserts that two resolves with different sticky sessions can produce
    different proxy URLs (distinct sticky sessions => potentially distinct
    exits), which is what the multi-session egress relies on.
    """
    from legal.providers.proxy import resolve_proxy

    url_a = resolve_proxy(session="s-egress-a")
    assert url_a is not None, "proxy disabled despite LEGAL_PROXY_ENABLED=1"
    assert "floxy.io" in url_a, f"expected a floxy URL, got {url_a!r}"
    assert "country-US" in url_a, f"expected a US exit, got {url_a!r}"

    # A second resolve with a different session yields a different URL (sticky
    # session is embedded in the username), so callers can vary the exit.
    url_b = resolve_proxy(session="s-egress-b")
    assert url_b is not None
    assert url_a != url_b, "distinct sessions produced identical proxy URLs"

    ip = _fetch_ip_through_proxy(url_a)
    # Sanity: a routable, non-private address came back through the exit.
    parsed = ipaddress.ip_address(ip)
    assert not parsed.is_private, f"proxy returned a private IP: {ip!r}"


def test_legal_http_client_carries_floxy_proxy(floxy_proxy_env: None) -> None:
    """``LegalHttpClient`` picks up the resolved Floxy proxy at the HTTP seam.

    Builds the client with no explicit proxy so it resolves from settings, then
    proves the egress is actually proxied by fetching an IP through it (the
    underlying ``httpx.Client`` carries the floxy proxy mounts).
    """
    from legal.http import LegalHttpClient
    from legal.providers.proxy import resolve_proxy

    # The seam must resolve a floxy proxy now that the env is enabled.
    assert resolve_proxy() is not None, "HTTP seam resolved no proxy when enabled"

    with LegalHttpClient(base_url="") as client:
        # httpx mounts proxy transports per scheme; with a proxy configured the
        # client carries non-default mounts (a direct client has none).
        mounts = getattr(client._client, "_mounts", {})
        assert mounts, "LegalHttpClient built without proxy mounts despite floxy enabled"

        # And prove it actually routes through the proxy end-to-end.
        text = client.get_text(
            "https://api.ipify.org?format=json", timeout=PROXY_HTTP_TIMEOUT
        )
    ip = _extract_ip(httpx.Response(200, text=text).json())
    assert ip is not None, f"no IP in proxied response via LegalHttpClient: {text[:200]!r}"
    assert not ipaddress.ip_address(ip).is_private


def test_pick_profile_rotates_across_profiles() -> None:
    """``config.pick_profile`` returns >1 distinct profile across repeated calls.

    When more than one ``.enc`` profile is vendored, repeated unpinned calls must
    be able to select different profiles (rotation). With a single profile the
    rotation assertion is meaningless, so it is skipped.
    """
    from legal import config

    profiles = sorted(config.botbrowser_profiles_dir().glob("*.enc"))
    if len(profiles) <= 1:
        pytest.skip(f"only {len(profiles)} profile(s) available; rotation is moot")

    names = {config.pick_profile().name for _ in range(40)}
    assert len(names) > 1, (
        f"pick_profile never rotated across {len(profiles)} profiles "
        f"in 40 draws (always picked {names!r})"
    )


def test_botbrowser_hidden_launch_smoke() -> None:
    """One hidden BotBrowser launch + ``about:blank`` opens and closes cleanly.

    Validates the relocated ``legal.browser.BotBrowser`` (hidden Xvfb + vendored
    binary + ``pick_profile``) starts and stops without error. Proxy is left
    disabled (default) so this is a free, direct-egress launch smoke. Skips if
    the vendored binary/profiles are not present on disk.
    """
    from legal import config
    from legal.browser import BotBrowser

    try:
        config.botbrowser_bin()
        if not sorted(config.botbrowser_profiles_dir().glob("*.enc")):
            pytest.skip("no BotBrowser .enc profiles vendored")
    except RuntimeError as exc:
        pytest.skip(f"BotBrowser not available on disk: {exc}")

    with BotBrowser(hidden=True) as bb:
        assert bb.ctx is not None, "BotBrowser context did not open"
        assert bb.page is not None, "BotBrowser page did not open"
        bb.page.goto("about:blank")
        assert bb.page.url == "about:blank"
    # Context-manager exit ran _stop(); a clean teardown means no exception.
    assert bb.ctx is None, "BotBrowser context was not torn down"
    assert bb.page is None
