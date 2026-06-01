"""Offline unit tests for the captcha provider layer.

No network: the Capsolver HTTP calls are mocked at ``_post_json``/``time.sleep``
so the pure solving logic (payload shaping, polling, error mapping) is exercised
without touching ``api.capsolver.com``.
"""

from __future__ import annotations

import pytest

import legal.captcha as captcha_shim
from legal.providers import captcha as provider
from legal.providers.captcha import CaptchaError, get_backend, list_backends
from legal.providers.captcha.capsolver import CapsolverBackend


# ---------------------------------------------------------------------------
# Registry / backend selection
# ---------------------------------------------------------------------------


def test_get_backend_default_is_capsolver():
    backend = get_backend()
    assert isinstance(backend, CapsolverBackend)
    assert backend.name == "capsolver"
    assert "capsolver" in list_backends()


def test_get_backend_respects_settings_provider(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPTCHA_PROVIDER", "capsolver")
    from legal.settings import reload_settings

    reload_settings()
    backend = get_backend()
    assert backend.name == "capsolver"


def test_get_backend_explicit_name():
    assert get_backend("capsolver").name == "capsolver"


def test_get_backend_unknown_raises_captcha_error():
    with pytest.raises(CaptchaError) as exc:
        get_backend("does-not-exist")
    assert "does-not-exist" in str(exc.value)


# ---------------------------------------------------------------------------
# Legacy shim identity
# ---------------------------------------------------------------------------


def test_legacy_captcha_error_is_provider_error():
    assert captcha_shim.CaptchaError is CaptchaError


def test_shim_solve_image_delegates_to_backend(monkeypatch):
    calls = {}

    class _Stub:
        def solve_image(self, image):
            calls["image"] = image
            return "STUB"

    monkeypatch.setattr(provider, "get_backend", lambda: _Stub())
    assert captcha_shim.solve_image("abc") == "STUB"
    assert calls["image"] == "abc"


def test_shim_solve_recaptcha_delegates_to_backend(monkeypatch):
    captured = {}

    class _Stub:
        def solve_recaptcha_v3(self, page_url, site_key, action, min_score, timeout_s):
            captured.update(
                page_url=page_url,
                site_key=site_key,
                action=action,
                min_score=min_score,
                timeout_s=timeout_s,
            )
            return "TOKEN"

    monkeypatch.setattr(provider, "get_backend", lambda: _Stub())
    out = captcha_shim.solve_recaptcha_v3(
        "https://x", "site", "act", min_score=0.5, timeout_s=10
    )
    assert out == "TOKEN"
    assert captured["page_url"] == "https://x"
    assert captured["site_key"] == "site"
    assert captured["action"] == "act"
    assert captured["min_score"] == 0.5
    assert captured["timeout_s"] == 10


# ---------------------------------------------------------------------------
# CapsolverBackend.solve_image (httpx mocked via _post_json)
# ---------------------------------------------------------------------------


def _patch_post_json(monkeypatch, responses):
    """Patch CapsolverBackend._post_json to return queued dicts and record calls."""
    seq = list(responses)
    log: list[tuple[str, dict]] = []

    def fake_post(self, client, path, payload, operation):
        log.append((path, payload))
        result = seq.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(CapsolverBackend, "_post_json", fake_post)
    return log


def test_solve_image_returns_text(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPSOLVER_API_KEY", "k-test")
    log = _patch_post_json(
        monkeypatch,
        [{"errorId": 0, "status": "ready", "solution": {"text": "hello"}}],
    )
    out = CapsolverBackend().solve_image("aGVsbG8=")
    assert out == "hello"
    # posted to createTask with an ImageToTextTask carrying the key
    path, payload = log[0]
    assert path == "/createTask"
    assert payload["clientKey"] == "k-test"
    assert payload["task"]["type"] == "ImageToTextTask"


def test_solve_image_error_id_raises(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPSOLVER_API_KEY", "k-test")
    _patch_post_json(monkeypatch, [{"errorId": 1, "errordescription": "bad"}])
    with pytest.raises(CaptchaError):
        CapsolverBackend().solve_image("aGVsbG8=")


def test_solve_image_missing_solution_raises(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPSOLVER_API_KEY", "k-test")
    _patch_post_json(monkeypatch, [{"errorId": 0, "status": "ready", "solution": {}}])
    with pytest.raises(CaptchaError):
        CapsolverBackend().solve_image("aGVsbG8=")


# ---------------------------------------------------------------------------
# CapsolverBackend.solve_recaptcha_v3 (polling mocked)
# ---------------------------------------------------------------------------


def test_solve_recaptcha_v3_builds_task_and_returns_token(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPSOLVER_API_KEY", "k-test")
    monkeypatch.setattr("legal.providers.captcha.capsolver.time.sleep", lambda *_: None)
    log = _patch_post_json(
        monkeypatch,
        [
            {"errorId": 0, "taskId": "T1"},
            {
                "errorId": 0,
                "status": "ready",
                "solution": {"gRecaptchaResponse": "TOK"},
            },
        ],
    )
    out = CapsolverBackend().solve_recaptcha_v3(
        "https://page", "SITEKEY", "login", min_score=0.7, timeout_s=30
    )
    assert out == "TOK"
    create_path, create_payload = log[0]
    assert create_path == "/createTask"
    task = create_payload["task"]
    assert task["type"] == "ReCaptchaV3TaskProxyless"
    assert task["websiteURL"] == "https://page"
    assert task["websiteKey"] == "SITEKEY"
    assert task["pageAction"] == "login"
    assert task["minScore"] == 0.7
    assert log[1][0] == "/getTaskResult"


def test_solve_recaptcha_v3_create_error_raises(monkeypatch):
    monkeypatch.setenv("LEGAL_CAPSOLVER_API_KEY", "k-test")
    monkeypatch.setattr("legal.providers.captcha.capsolver.time.sleep", lambda *_: None)
    _patch_post_json(monkeypatch, [{"errorId": 1, "errorDescription": "nope"}])
    with pytest.raises(CaptchaError):
        CapsolverBackend().solve_recaptcha_v3("https://p", "s", "a", timeout_s=30)
