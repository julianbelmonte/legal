"""Map pipeline results and errors to HTTP status while echoing the envelope.

The API stays 1:1 with the CLI: the response body is always the same normalized
JSON envelope the CLI prints. We only *choose* an HTTP status from the error
code; we never reshape the body.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from legal.errors import LegalCliError
from legal.models import LegalResponse

__all__ = ["STATUS_BY_CODE", "envelope_and_status", "error_to_envelope"]

# Error code -> HTTP status. Anything not listed (and any ``ok: True`` result)
# resolves to 200 while still returning the normalized envelope body.
STATUS_BY_CODE: dict[str, int] = {
    "usage_error": 400,
    "not_found": 404,
    "unsupported_operation": 422,
    "unsupported_captcha": 422,
    "network_error": 502,
    "source_unavailable": 502,
}


def _to_dict(result: LegalResponse | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, LegalResponse):
        return result.to_dict()
    if isinstance(result, Mapping):
        return dict(result)
    raise TypeError(f"expected LegalResponse or Mapping, got {type(result).__name__}")


def envelope_and_status(result: LegalResponse | Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """Return ``(envelope_dict, http_status)`` for a pipeline result.

    Status is 200 when the envelope's ``ok`` is truthy; otherwise it is derived
    from the error code via :data:`STATUS_BY_CODE` (default 200).
    """
    env = _to_dict(result)
    if env.get("ok"):
        return env, 200
    error = env.get("error") or {}
    code = error.get("code") if isinstance(error, Mapping) else None
    return env, STATUS_BY_CODE.get(code, 200)


def error_to_envelope(
    exc: LegalCliError,
    *,
    source: str,
    operation: str,
) -> tuple[dict[str, Any], int]:
    """Convert a raised :class:`LegalCliError` into ``(envelope_dict, status)``."""
    env = exc.to_response(source=source, operation=operation).to_dict()
    return env, STATUS_BY_CODE.get(exc.code, 200)
