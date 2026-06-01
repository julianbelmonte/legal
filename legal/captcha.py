"""Backward-compatible captcha shim for the standalone legal CLI.

The real solving logic lives in the captcha provider layer
(``legal.providers.captcha``). This module preserves the historical public API
(``CaptchaError``, ``solve_image``, ``solve_recaptcha_v3``) so the existing
source adapters keep importing and catching exactly as before; the function
bodies simply delegate to the configured backend.
"""

from __future__ import annotations

from legal.providers import captcha as _provider
from legal.providers.captcha import CaptchaError


def solve_image(image: str) -> str:
    """Solve a base64 captcha image via the configured captcha backend."""

    return _provider.get_backend().solve_image(image)


def solve_recaptcha_v3(
    page_url: str,
    site_key: str,
    action: str,
    min_score: float = 0.3,
    timeout_s: float = 180,
) -> str:
    """Solve a reCAPTCHA v3 task via the configured captcha backend."""

    return _provider.get_backend().solve_recaptcha_v3(
        page_url,
        site_key,
        action,
        min_score=min_score,
        timeout_s=timeout_s,
    )


__all__ = ["CaptchaError", "solve_image", "solve_recaptcha_v3"]
