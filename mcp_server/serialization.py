"""JSON envelope serialization helpers for the MCP server.

The MCP server returns JSON-compatible normalized envelopes that preserve the
pipeline's envelope keys (``ok``, ``source``, ``operation``, ``query``,
``document``, ``page``, ``provenance``, ``warnings``, ``error``).

`legal.dispatch.run_operation` may return a :class:`LegalResponse`, a plain
mapping, or other dataclass-backed structures. These helpers normalize any of
those into stable JSON-compatible dictionaries without mutating the underlying
envelopes, and reject values that cannot be represented in JSON by raising a
:class:`SerializationError` that callers can render as an MCP-friendly error
envelope.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["SerializationError", "to_jsonable", "error_envelope"]


class SerializationError(TypeError):
    """Raised when a value cannot be converted to a JSON-compatible form."""


def to_jsonable(value: Any) -> Any:
    """Convert a pipeline value into a JSON-compatible structure.

    Objects exposing a ``to_dict`` method (such as :class:`LegalResponse`) are
    serialized through it so the normalized envelope shape is preserved.
    Dataclasses, mappings, and sequences are converted recursively. Dates,
    paths, and enums are coerced to their JSON forms. Any value that is not
    JSON-serializable raises :class:`SerializationError`.
    """
    return _convert(value)


def _convert(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict) and not isinstance(value, type):
        return _convert(to_dict())

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _convert(getattr(value, item.name))
            for item in fields(value)
            if getattr(value, item.name) is not None
        }

    if isinstance(value, Mapping):
        return {str(key): _convert(item) for key, item in value.items()}

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, Enum):
        return _convert(value.value)

    if isinstance(value, (bytes, bytearray)):
        raise SerializationError(
            f"binary value of type {type(value).__name__} is not JSON-serializable"
        )

    if isinstance(value, Sequence):
        return [_convert(item) for item in value]

    if isinstance(value, (set, frozenset)):
        return [_convert(item) for item in value]

    raise SerializationError(
        f"value of type {type(value).__name__} is not JSON-serializable"
    )


def error_envelope(
    *,
    source: str,
    operation: str,
    message: str,
    code: str = "serialization_error",
) -> dict[str, Any]:
    """Build a normalized MCP-friendly error envelope.

    Used when a pipeline result cannot be serialized, so the MCP tool can still
    return a stable JSON document instead of failing opaquely.
    """
    return {
        "ok": False,
        "source": source,
        "operation": operation,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
        },
    }
