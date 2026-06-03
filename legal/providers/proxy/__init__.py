"""Proxy provider registry and the ``resolve_proxy()`` egress helper.

Mirrors the webcam ``drone/network/__init__`` registry pattern (without the
accounting wrapper, which we do not bring). The egress seams — the HTTP client
and the browser launcher — never touch providers directly; they ask
:func:`resolve_proxy` (or :func:`resolve_proxy_for_playwright`) for "the proxy to
use right now, or None". That helper consults ``legal.settings``: when the proxy
is disabled or the provider is ``"none"`` it returns ``None`` (direct egress,
current behavior); otherwise it builds a URL from the selected backend for
``settings.proxy_country``. No network calls happen at import time.
"""

from __future__ import annotations

from .base import (
    ProxyBackend,
    generate_session_id,
    parse_proxy_for_playwright,
    parse_proxy_url,
)
from .anyip import AnyIPBackend
from .floxy import FloxyBackend
from .none import NullProxyBackend

_BACKENDS: dict[str, ProxyBackend] = {}


def _register(backend: ProxyBackend) -> None:
    _BACKENDS[backend.name] = backend


_register(AnyIPBackend())
_register(FloxyBackend())
_register(NullProxyBackend())


def get_backend(name: str) -> ProxyBackend:
    """Return the proxy backend registered under ``name``.

    Raises ``KeyError`` for an unknown provider name.
    """

    try:
        return _BACKENDS[name]
    except KeyError as exc:
        raise KeyError(
            f"unknown proxy provider: {name!r} (available: {list_backends()})"
        ) from exc


def list_backends() -> list[str]:
    return list(_BACKENDS.keys())


def detect_backend(proxy_url: str) -> ProxyBackend | None:
    """Return the first registered backend that claims *proxy_url*, or ``None``."""

    for backend in _BACKENDS.values():
        if backend.matches_url(proxy_url):
            return backend
    return None


def resolve_proxy(session: str | None = None) -> str | None:
    """Return the proxy URL to use right now, or ``None`` for direct egress.

    Consults ``legal.settings.get_settings()``: returns ``None`` when the proxy
    is disabled or the provider is ``"none"``; otherwise builds a URL from the
    selected backend for ``settings.proxy_country``, with an optional sticky
    *session*.
    """

    from legal.settings import get_settings

    settings = get_settings()
    if not settings.proxy_enabled or settings.proxy_provider == "none":
        return None
    backend = get_backend(settings.proxy_provider)
    return backend.build_proxy_url(settings.proxy_country, session=session)


def resolve_proxy_for_playwright(session: str | None = None) -> dict | None:
    """Return the Playwright proxy dict to use right now, or ``None``."""

    proxy_url = resolve_proxy(session=session)
    if proxy_url is None:
        return None
    return parse_proxy_for_playwright(proxy_url)


__all__ = [
    "AnyIPBackend",
    "FloxyBackend",
    "NullProxyBackend",
    "ProxyBackend",
    "detect_backend",
    "generate_session_id",
    "get_backend",
    "list_backends",
    "parse_proxy_for_playwright",
    "parse_proxy_url",
    "resolve_proxy",
    "resolve_proxy_for_playwright",
]
