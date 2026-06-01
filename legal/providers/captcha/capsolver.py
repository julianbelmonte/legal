"""Capsolver captcha backend.

Relocates the battle-tested Capsolver solving logic (formerly in
``legal/captcha.py``) behind the provider-agnostic ``CaptchaBackend`` interface.
Endpoints, timeouts, polling constants, request payloads, and error message text
are preserved exactly. The client key is read from
``legal.config.capsolver_api_key()`` (which resolves via the secret chain), and
failures raise the provider-agnostic errors from ``base`` while keeping the
message strings identical.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from legal.config import capsolver_api_key

from .base import CaptchaBackend, CaptchaError


BASE_URL = "https://api.capsolver.com"
REQUEST_TIMEOUT_S = 30.0
RECAPTCHA_INITIAL_WAIT_S = 5.0
RECAPTCHA_POLL_INTERVAL_S = 3.0


class CapsolverBackend(CaptchaBackend):
    """Capsolver-backed captcha solver."""

    name = "capsolver"

    BASE_URL = "https://api.capsolver.com"
    REQUEST_TIMEOUT_S = 30.0
    RECAPTCHA_INITIAL_WAIT_S = 5.0
    RECAPTCHA_POLL_INTERVAL_S = 3.0

    def solve_image(self, image_b64: str) -> str:
        """Solve a base64 captcha image via Capsolver ImageToTextTask."""

        body = self._normalize_image_body(image_b64)
        payload = {
            "clientKey": capsolver_api_key(),
            "task": {
                "type": "ImageToTextTask",
                "body": body,
            },
        }
        with httpx.Client(timeout=self.REQUEST_TIMEOUT_S) as client:
            data = self._post_json(client, "/createTask", payload, "createTask")

        if data.get("errorId"):
            raise CaptchaError(f"capsolver createTask error: {data}")
        if data.get("status") != "ready":
            raise CaptchaError(f"capsolver image task was not ready: {data}")

        try:
            text = data["solution"]["text"]
        except KeyError as exc:
            raise CaptchaError(f"capsolver image response missing solution text: {data}") from exc
        if not isinstance(text, str) or not text:
            raise CaptchaError(f"capsolver image response missing solution text: {data}")
        return text

    def solve_recaptcha_v3(
        self,
        page_url: str,
        site_key: str,
        action: str,
        min_score: float = 0.3,
        timeout_s: float = 180,
    ) -> str:
        """Solve a reCAPTCHA v3 task and return the provider token."""

        client_key = capsolver_api_key()
        task = {
            "type": "ReCaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
            "minScore": min_score,
        }
        with httpx.Client(timeout=self.REQUEST_TIMEOUT_S) as client:
            data = self._post_json(
                client,
                "/createTask",
                {"clientKey": client_key, "task": task},
                "createTask",
            )
            if data.get("errorId"):
                raise CaptchaError(f"capsolver createTask error: {data}")
            try:
                task_id = data["taskId"]
            except KeyError as exc:
                raise CaptchaError(f"capsolver createTask response missing taskId: {data}") from exc

            deadline = time.monotonic() + timeout_s
            if timeout_s > 0:
                time.sleep(min(self.RECAPTCHA_INITIAL_WAIT_S, timeout_s))

            while time.monotonic() < deadline:
                result = self._post_json(
                    client,
                    "/getTaskResult",
                    {"clientKey": client_key, "taskId": task_id},
                    "getTaskResult",
                )
                if result.get("errorId"):
                    raise CaptchaError(f"capsolver getTaskResult error: {result}")
                if result.get("status") == "ready":
                    try:
                        token = result["solution"]["gRecaptchaResponse"]
                    except KeyError as exc:
                        raise CaptchaError(
                            f"capsolver recaptcha response missing token: {result}"
                        ) from exc
                    if not isinstance(token, str) or not token:
                        raise CaptchaError(
                            f"capsolver recaptcha response missing token: {result}"
                        )
                    return token
                time.sleep(
                    min(self.RECAPTCHA_POLL_INTERVAL_S, max(0.0, deadline - time.monotonic()))
                )

        raise CaptchaError("capsolver solve timed out")

    def _normalize_image_body(self, image: str) -> str:
        value = image.strip()
        if value.startswith("data:"):
            prefix, sep, payload = value.partition(",")
            if not sep or "base64" not in "".join(prefix.lower().split()):
                raise CaptchaError("captcha image data URI is not base64 encoded")
            value = payload
        elif "base64," in value:
            value = value.split("base64,", 1)[1]

        value = "".join(value.split())
        if not value:
            raise CaptchaError("captcha image is empty")
        return value + "=" * (-len(value) % 4)

    def _post_json(
        self,
        client: httpx.Client,
        path: str,
        payload: dict[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = client.post(f"{self.BASE_URL}{path}", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise CaptchaError(f"capsolver {operation} request failed: {exc}") from exc
        except ValueError as exc:
            raise CaptchaError(f"capsolver {operation} returned invalid JSON") from exc

        if not isinstance(data, dict):
            raise CaptchaError(f"capsolver {operation} returned non-object JSON: {data!r}")
        return data


__all__ = ["CapsolverBackend"]
