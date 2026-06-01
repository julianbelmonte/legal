"""Proxy backend ABC and shared utilities.

Standalone, minimal reimplementation of the webcam ``drone/network`` proxy
pattern. No ``drone`` import, no accounting, geo-reputation rotation, or VPN
backends — just enough to build a proxy URL for a country and convert it to
Playwright's proxy dict. No network calls happen at import or in these helpers.
"""

import random
import string
from abc import ABC, abstractmethod

_SESSION_CHARS = string.ascii_letters + string.digits


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def generate_session_id(length: int = 10) -> str:
    """Return a random alphanumeric sticky-session identifier."""
    return "".join(random.choices(_SESSION_CHARS, k=length))


def parse_proxy_url(proxy_url: str) -> tuple[str, str, str]:
    """Parse ``http://user:pass@host:port`` into ``(user, password, server)``."""
    part = proxy_url.split("://", 1)[-1]
    creds, server = part.rsplit("@", 1)
    user, password = creds.split(":", 1)
    return user, password, server


def parse_proxy_for_playwright(proxy_url: str) -> dict:
    """Convert a ``http://user:pass@host:port`` URL to Playwright's proxy dict.

    Returns ``{"server": "http://host:port", "username": ..., "password": ...}``.
    ``username``/``password`` keys are only included when present.
    """
    user, password, server = parse_proxy_url(proxy_url)
    if "://" not in server:
        server = f"http://{server}"
    pw_proxy: dict[str, str] = {"server": server}
    if user:
        pw_proxy["username"] = user
    if password:
        pw_proxy["password"] = password
    return pw_proxy


# ---------------------------------------------------------------------------
# ProxyBackend ABC
# ---------------------------------------------------------------------------

class ProxyBackend(ABC):
    """Abstract proxy backend.

    Subclasses set the class attribute *name* and implement
    :meth:`build_proxy_url` and :meth:`matches_url`.
    """

    name: str

    @abstractmethod
    def build_proxy_url(self, country_code: str, session: str | None = None) -> str:
        """Build a proxy URL for *country_code*, optionally with a sticky *session*."""

    @abstractmethod
    def matches_url(self, proxy_url: str) -> bool:
        """Return ``True`` if *proxy_url* belongs to this backend."""
