"""Opaque cursors for document text page navigation.

Document text must never be silently truncated. When a document's extracted
text exceeds one page, the MCP ``legal_get_document_text`` tool returns an
opaque cursor that encodes everything needed to resume reading: the cache id of
the stored record, its source, the resolver operation, the character offset, the
page limit, and a cursor schema version.

These cursors are built on top of :mod:`legal.pagination` so they share the
project's existing URL-safe base64 JSON cursor style rather than inventing a new
opaque format. This module adds the document-specific shape and validation on
top of that shared encoder/decoder: cursors are rejected when they carry the
wrong operation, a negative offset, an out-of-range limit (``<= 0`` or above the
MCP ``max_page_size``), an unknown/expired cache id (when a cache is provided to
check against), or a malformed payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from legal.pagination import (
    CURSOR_VERSION,
    decode_cursor,
    encode_cursor,
)
from mcp_server.document_text.cache import DocumentTextCache
from mcp_server.settings import get_mcp_settings

JsonDict = dict[str, Any]

# The operation tag carried by every document text cursor. Validation rejects
# cursors whose operation does not match, so a search/document cursor from
# elsewhere in the pipeline cannot be replayed against the document text tool.
DOCUMENT_TEXT_OPERATION = "document_text"

# Cursor schema version for document text cursors. Bump when the payload shape
# changes incompatibly.
DOCUMENT_TEXT_CURSOR_VERSION = CURSOR_VERSION


class DocumentTextCursorError(ValueError):
    """Raised when a document text cursor is malformed or invalid."""


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DocumentTextCursorError(
            f"document text cursor {field} must be a non-empty string"
        )
    return value


def _require_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DocumentTextCursorError(
            f"document text cursor {field} must be an integer"
        )
    return value


def _max_page_size() -> int:
    return get_mcp_settings().max_page_size


def make_document_text_cursor(
    *,
    cache_id: str,
    source: str,
    offset: int,
    limit: int,
    operation: str = DOCUMENT_TEXT_OPERATION,
    version: int = DOCUMENT_TEXT_CURSOR_VERSION,
) -> str:
    """Encode an opaque document text cursor.

    Encodes the cache id, source, operation, offset, limit, and version using
    the shared :mod:`legal.pagination` cursor encoder. The inputs are validated
    so a cursor can never be minted with a negative offset or an out-of-range
    limit.
    """
    _require_text(cache_id, field="cache_id")
    _require_text(source, field="source")
    operation = _require_text(operation, field="operation")
    offset = _require_int(offset, field="offset")
    limit = _require_int(limit, field="limit")
    version = _require_int(version, field="version")

    if operation != DOCUMENT_TEXT_OPERATION:
        raise DocumentTextCursorError(
            f"document text cursor operation must be {DOCUMENT_TEXT_OPERATION!r}, "
            f"not {operation!r}"
        )
    if offset < 0:
        raise DocumentTextCursorError(
            "document text cursor offset must be greater than or equal to 0"
        )
    _validate_limit(limit)
    if version < 1:
        raise DocumentTextCursorError(
            "document text cursor version must be greater than or equal to 1"
        )

    payload: JsonDict = {
        "source": source,
        "operation": operation,
        "cache_id": cache_id,
        "offset": offset,
        "limit": limit,
        "version": version,
    }
    return encode_cursor(payload)


def _validate_limit(limit: int) -> None:
    if limit <= 0:
        raise DocumentTextCursorError(
            "document text cursor limit must be greater than 0"
        )
    maximum = _max_page_size()
    if limit > maximum:
        raise DocumentTextCursorError(
            f"document text cursor limit {limit} exceeds the maximum page size {maximum}"
        )


def decode_document_text_cursor(
    cursor: str,
    *,
    source: str | None = None,
    cache: DocumentTextCache | None = None,
    now: datetime | None = None,
) -> JsonDict:
    """Decode and validate an opaque document text cursor.

    Returns the cursor payload (with at least ``cache_id``, ``source``,
    ``offset``, ``limit``, plus ``operation`` and ``version``). Raises
    :class:`DocumentTextCursorError` for any malformed or invalid cursor:

    - not a non-empty base64url JSON object cursor;
    - wrong operation tag;
    - mismatched ``source`` (when one is supplied to check against);
    - missing/negative offset, missing/out-of-range limit;
    - an unknown or expired cache id (when ``cache`` is supplied to check).
    """
    try:
        payload = decode_cursor(
            cursor,
            source=source,
            operation=DOCUMENT_TEXT_OPERATION,
        )
    except ValueError as exc:
        raise DocumentTextCursorError(f"invalid document text cursor: {exc}") from exc

    cache_id = _require_text(payload.get("cache_id"), field="cache_id")
    cursor_source = _require_text(payload.get("source"), field="source")
    operation = _require_text(payload.get("operation"), field="operation")
    offset = _require_int(payload.get("offset"), field="offset")
    limit = _require_int(payload.get("limit"), field="limit")

    if operation != DOCUMENT_TEXT_OPERATION:
        raise DocumentTextCursorError(
            f"document text cursor operation must be {DOCUMENT_TEXT_OPERATION!r}, "
            f"not {operation!r}"
        )
    if source is not None and cursor_source != source:
        raise DocumentTextCursorError(
            f"document text cursor belongs to source {cursor_source!r}, not {source!r}"
        )
    if offset < 0:
        raise DocumentTextCursorError(
            "document text cursor offset must be greater than or equal to 0"
        )
    _validate_limit(limit)

    if cache is not None:
        record = cache.get(cache_id, now=now)
        if record is None:
            raise DocumentTextCursorError(
                f"document text cursor cache id {cache_id!r} is unknown or expired"
            )

    return payload


__all__ = [
    "DOCUMENT_TEXT_CURSOR_VERSION",
    "DOCUMENT_TEXT_OPERATION",
    "DocumentTextCursorError",
    "decode_document_text_cursor",
    "make_document_text_cursor",
]
