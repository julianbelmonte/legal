"""Captcha provider registry and default backend selection.

Mirrors the webcam ``drone/captcha/__init__`` registry pattern (without the
accounting wrapper, which we do not bring). The active backend is chosen by
``legal.settings.get_settings().captcha_provider`` (default ``capsolver``), so
deploys can swap captcha providers without touching adapters.
"""

from __future__ import annotations

from .base import (
    CaptchaBackend,
    CaptchaBalanceError,
    CaptchaDescriptor,
    CaptchaError,
    CaptchaSolution,
    CaptchaSolveFailed,
    CaptchaTimeout,
    CaptchaType,
    CaptchaUnsupported,
)
from .capsolver import CapsolverBackend

_BACKENDS: dict[str, CaptchaBackend] = {}


def _register(backend: CaptchaBackend) -> None:
    _BACKENDS[backend.name] = backend


_register(CapsolverBackend())


def get_backend(name: str | None = None) -> CaptchaBackend:
    """Return the captcha backend for ``name`` (default from settings).

    When ``name`` is None, read the active provider from
    ``legal.settings.get_settings().captcha_provider``. Raises ``CaptchaError``
    for an unknown provider name.
    """

    if name is None:
        from legal.settings import get_settings

        name = get_settings().captcha_provider
    try:
        return _BACKENDS[name]
    except KeyError as exc:
        raise CaptchaError(
            f"unknown captcha provider: {name!r} (available: {list_backends()})"
        ) from exc


def list_backends() -> list[str]:
    return list(_BACKENDS.keys())


__all__ = [
    "CapsolverBackend",
    "CaptchaBackend",
    "CaptchaBalanceError",
    "CaptchaDescriptor",
    "CaptchaError",
    "CaptchaSolution",
    "CaptchaSolveFailed",
    "CaptchaTimeout",
    "CaptchaType",
    "CaptchaUnsupported",
    "get_backend",
    "list_backends",
]
