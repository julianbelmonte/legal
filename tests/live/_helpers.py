"""Shared helpers for the live test tier.

These helpers run only under ``LEGAL_LIVE=1`` (see the root ``conftest`` gating).
They hit real sources, some of which spend Capsolver credits / proxy bandwidth,
so tests use the credential gates in ``tests/live/conftest.py`` to skip cleanly
when secrets are absent and apply per-call timeouts so a hung source fails
rather than blocking the suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

#: Default per-call timeout (seconds) for the subprocess CLI helper. Browser /
#: captcha-backed sources are slow, so this is generous but still bounded so a
#: genuinely hung source fails rather than blocking the whole suite.
CLI_TIMEOUT_SECONDS = 240


def _envelope(payload: Any) -> Mapping[str, Any]:
    """Coerce a response (envelope object or mapping) to a plain mapping."""
    if isinstance(payload, Mapping):
        return payload
    # ``LegalResponse`` and similar expose ``to_dict``; fall back to ``__dict__``.
    to_dict = getattr(payload, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return result
    as_dict = getattr(payload, "__dict__", None)
    if isinstance(as_dict, Mapping):
        return as_dict
    raise AssertionError(
        f"response is not a normalized envelope (type={type(payload)!r}): {payload!r}"
    )


def assert_ok_envelope(payload: Any) -> Mapping[str, Any]:
    """Assert ``payload`` is a normalized envelope with ``ok`` true.

    Accepts either a mapping or a ``LegalResponse``-like object. For ``ok:false``
    the assertion message surfaces ``error.code`` and ``error.message`` so live
    failures are immediately actionable. Returns the envelope as a mapping.
    """
    env = _envelope(payload)
    assert "ok" in env, f"response has no 'ok' field: {env!r}"
    if not env.get("ok"):
        error = env.get("error") or {}
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message")
        else:
            code = message = None
        raise AssertionError(
            f"expected ok envelope but got ok=false "
            f"(error.code={code!r}, error.message={message!r})"
        )
    return env


def cli(*args: str, timeout: float = CLI_TIMEOUT_SECONDS) -> Mapping[str, Any]:
    """Run ``python -m legal.cli <args>`` via subprocess and return parsed JSON.

    The CLI always prints exactly one JSON document to stdout. Raises on a
    non-JSON stdout (surfacing stderr) or on timeout so a hung source fails.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "legal.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = proc.stdout.strip()
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"CLI did not emit JSON (rc={proc.returncode}); "
            f"stdout={stdout!r} stderr={proc.stderr.strip()!r}"
        ) from exc


def dispatch(source: str, op: str, **params: Any) -> Mapping[str, Any]:
    """Call ``legal.dispatch.run_operation`` directly and return its envelope.

    The in-process counterpart to :func:`cli`; returns the normalized envelope
    as a mapping. Live tests may use either entry point.
    """
    from legal.dispatch import run_operation

    response = run_operation(source, op, params or None)
    return _envelope(response)
