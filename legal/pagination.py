"""Pagination helpers shared by legal source adapters."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from apps.legal.models import PageInfo


JsonDict = dict[str, Any]

CURSOR_VERSION = 1


def _copy_payload(payload: Mapping[str, Any]) -> JsonDict:
    if not isinstance(payload, Mapping):
        raise TypeError("cursor payload must be a mapping")
    return dict(payload)


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"cursor {field} must be a non-empty string")
    return value


def _validate_int(payload: Mapping[str, Any], field: str, *, minimum: int) -> None:
    if field not in payload or payload[field] is None:
        return
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"cursor {field} must be an integer")
    if value < minimum:
        raise ValueError(f"cursor {field} must be greater than or equal to {minimum}")


def _cursor_b64decode(cursor: str) -> bytes:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty string")
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("cursor is not valid base64url") from exc


def encode_cursor(payload: Mapping[str, Any]) -> str:
    """Encode a JSON cursor payload as URL-safe base64."""
    raw = json.dumps(_copy_payload(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(
    cursor: str,
    *,
    source: str | None = None,
    operation: str | None = None,
) -> JsonDict:
    """Decode a cursor and optionally validate its source and operation."""
    try:
        raw = _cursor_b64decode(cursor).decode("utf-8")
        payload = json.loads(raw)
    except UnicodeDecodeError as exc:
        raise ValueError("cursor payload is not utf-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("cursor payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("cursor payload must be a JSON object")
    return validate_cursor(payload, source=source, operation=operation)


def validate_cursor(
    payload: Mapping[str, Any],
    *,
    source: str | None = None,
    operation: str | None = None,
) -> JsonDict:
    """Validate a decoded cursor against the requested source and operation."""
    normalized = _copy_payload(payload)
    if source is not None:
        cursor_source = _require_text(normalized.get("source"), field="source")
        if cursor_source != source:
            raise ValueError(f"cursor belongs to source {cursor_source!r}, not {source!r}")
    if operation is not None:
        cursor_operation = _require_text(normalized.get("operation"), field="operation")
        if cursor_operation != operation:
            raise ValueError(f"cursor belongs to operation {cursor_operation!r}, not {operation!r}")

    _validate_int(normalized, "version", minimum=1)
    _validate_int(normalized, "page", minimum=0)
    _validate_int(normalized, "offset", minimum=0)
    _validate_int(normalized, "limit", minimum=1)
    if "search_id" in normalized and normalized["search_id"] is not None:
        _require_text(normalized["search_id"], field="search_id")
    return normalized


def cursor_payload(
    *,
    source: str,
    operation: str,
    page: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
    raw: Any = None,
    search_id: str | None = None,
    version: int | None = CURSOR_VERSION,
) -> JsonDict:
    """Build a normalized cursor payload for stateless or stateful continuation."""
    _require_text(source, field="source")
    _require_text(operation, field="operation")
    payload: JsonDict = {"source": source, "operation": operation}
    if version is not None:
        payload["version"] = version
    if page is not None:
        payload["page"] = page
    if offset is not None:
        payload["offset"] = offset
    if limit is not None:
        payload["limit"] = limit
    if raw is not None:
        payload["raw"] = raw
    if search_id is not None:
        payload["search_id"] = search_id
    return validate_cursor(payload, source=source, operation=operation)


def make_cursor(
    *,
    source: str,
    operation: str,
    page: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
    raw: Any = None,
    search_id: str | None = None,
) -> str:
    """Encode a normalized cursor payload."""
    return encode_cursor(
        cursor_payload(
            source=source,
            operation=operation,
            page=page,
            offset=offset,
            limit=limit,
            raw=raw,
            search_id=search_id,
        )
    )


def _infer_has_more(
    *,
    total: int | None,
    item_count: int | None,
    limit: int | None,
    offset: int | None,
    page: int | None,
    next_offset: int | None,
    next_page: int | None,
    raw: Any,
    search_id: str | None,
) -> bool:
    if next_offset is not None or next_page is not None or raw is not None or search_id is not None:
        return True
    if total is None or limit is None:
        return False
    if offset is not None:
        consumed = offset + (item_count if item_count is not None else limit)
        return consumed < total
    if page is not None:
        consumed = (page + 1) * limit if page == 0 else page * limit
        return consumed < total
    return False


def _next_offset(offset: int | None, limit: int | None, next_offset: int | None) -> int | None:
    if next_offset is not None:
        return next_offset
    if offset is None or limit is None:
        return None
    return offset + limit


def _next_page(page: int | None, next_page: int | None) -> int | None:
    if next_page is not None:
        return next_page
    if page is None:
        return None
    return page + 1


def build_page_info(
    *,
    source: str,
    operation: str,
    limit: int | None = None,
    offset: int | None = None,
    page: int | None = None,
    total: int | None = None,
    item_count: int | None = None,
    has_more: bool | None = None,
    next_offset: int | None = None,
    next_page: int | None = None,
    raw: Any = None,
    search_id: str | None = None,
    next_cursor: str | None = None,
) -> PageInfo:
    """Build PageInfo and generate a stateless next cursor when possible."""
    has_more_value = (
        has_more
        if has_more is not None
        else _infer_has_more(
            total=total,
            item_count=item_count,
            limit=limit,
            offset=offset,
            page=page,
            next_offset=next_offset,
            next_page=next_page,
            raw=raw,
            search_id=search_id,
        )
    )

    if has_more_value and next_cursor is None and search_id is None:
        next_cursor = make_cursor(
            source=source,
            operation=operation,
            page=_next_page(page, next_page),
            offset=_next_offset(offset, limit, next_offset),
            limit=limit,
            raw=raw,
        )

    return PageInfo(
        limit=limit,
        offset=offset,
        page=page,
        total=total,
        has_more=has_more_value,
        next_cursor=next_cursor,
        search_id=search_id,
    )


def page_info_from_offset(
    *,
    source: str,
    operation: str,
    offset: int,
    limit: int,
    total: int | None = None,
    item_count: int | None = None,
    raw: Any = None,
    has_more: bool | None = None,
) -> PageInfo:
    """Build PageInfo for offset-based source APIs."""
    return build_page_info(
        source=source,
        operation=operation,
        offset=offset,
        limit=limit,
        total=total,
        item_count=item_count,
        raw=raw,
        has_more=has_more,
    )


def page_info_from_page(
    *,
    source: str,
    operation: str,
    page: int,
    limit: int | None = None,
    total: int | None = None,
    item_count: int | None = None,
    raw: Any = None,
    has_more: bool | None = None,
) -> PageInfo:
    """Build PageInfo for page-number based source APIs."""
    return build_page_info(
        source=source,
        operation=operation,
        page=page,
        limit=limit,
        total=total,
        item_count=item_count,
        raw=raw,
        has_more=has_more,
    )


def page_info_from_search_state(
    *,
    search_id: str,
    limit: int | None = None,
    offset: int | None = None,
    page: int | None = None,
    total: int | None = None,
    has_more: bool = True,
) -> PageInfo:
    """Build PageInfo for stateful flows continued by a cached search id."""
    _require_text(search_id, field="search_id")
    return PageInfo(
        limit=limit,
        offset=offset,
        page=page,
        total=total,
        has_more=has_more,
        search_id=search_id,
    )


__all__ = [
    "CURSOR_VERSION",
    "JsonDict",
    "build_page_info",
    "cursor_payload",
    "decode_cursor",
    "encode_cursor",
    "make_cursor",
    "page_info_from_offset",
    "page_info_from_page",
    "page_info_from_search_state",
    "validate_cursor",
]
