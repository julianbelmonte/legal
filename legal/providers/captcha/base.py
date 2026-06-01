"""Captcha backend ABC, shared types, and shared error classes.

Provider-agnostic captcha abstraction for the legal pipeline. Kept minimal but
sufficient for the two capabilities the legal adapters use today: image-to-text
and reCAPTCHA v3. Mirrors the webcam ``drone/captcha/base`` pattern without
importing it, so additional backends can be added without touching adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class CaptchaType(str, Enum):
    IMAGE = "image"
    RECAPTCHA_V3 = "recaptcha_v3"


# ---------------------------------------------------------------------------
# Error hierarchy (provider-agnostic names)
# ---------------------------------------------------------------------------


class CaptchaError(Exception):
    """Base error for all captcha backend failures."""


class CaptchaUnsupported(CaptchaError):
    """Captcha type is not supported by the selected backend."""


class CaptchaTimeout(CaptchaError):
    """Captcha solve timed out."""


class CaptchaSolveFailed(CaptchaError):
    """Captcha provider could not solve the challenge."""


class CaptchaBalanceError(CaptchaError):
    """Captcha provider account balance is too low."""


@dataclass
class CaptchaDescriptor:
    type: CaptchaType
    page_url: str | None = None
    site_key: str | None = None
    image_b64: str | None = None
    action: str | None = None
    min_score: float | None = None


@dataclass
class CaptchaSolution:
    token: str
    backend: str
    raw: dict = field(default_factory=dict)


class CaptchaBackend(ABC):
    """Abstract captcha solving backend."""

    name: str

    @abstractmethod
    def solve_image(self, image_b64: str) -> str:
        """Solve an image-to-text captcha and return the recognized text."""

    @abstractmethod
    def solve_recaptcha_v3(
        self,
        page_url: str,
        site_key: str,
        action: str,
        min_score: float = 0.3,
        timeout_s: float = 180.0,
    ) -> str:
        """Solve an invisible reCAPTCHA v3 challenge and return the token."""
