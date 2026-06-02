"""Unit tests for MCP document text cursors."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.document_text.cache import DocumentTextCache
from mcp_server.document_text.cursors import (
    DOCUMENT_TEXT_OPERATION,
    DocumentTextCursorError,
    decode_document_text_cursor,
    make_document_text_cursor,
)
from mcp_server.settings import get_mcp_settings


def test_cursor_round_trip() -> None:
    cursor = make_document_text_cursor(
        cache_id="abc", source="csjn", offset=10, limit=20
    )
    payload = decode_document_text_cursor(cursor)
    assert payload["cache_id"] == "abc"
    assert payload["source"] == "csjn"
    assert payload["operation"] == DOCUMENT_TEXT_OPERATION
    assert payload["offset"] == 10
    assert payload["limit"] == 20
    assert payload["version"] >= 1


def test_decode_validates_source() -> None:
    cursor = make_document_text_cursor(
        cache_id="abc", source="csjn", offset=0, limit=5
    )
    with pytest.raises(DocumentTextCursorError):
        decode_document_text_cursor(cursor, source="saij")


def test_make_rejects_negative_offset() -> None:
    with pytest.raises(DocumentTextCursorError):
        make_document_text_cursor(cache_id="abc", source="csjn", offset=-1, limit=5)


def test_make_rejects_non_positive_limit() -> None:
    with pytest.raises(DocumentTextCursorError):
        make_document_text_cursor(cache_id="abc", source="csjn", offset=0, limit=0)


def test_make_rejects_limit_above_max_page_size() -> None:
    maximum = get_mcp_settings().max_page_size
    with pytest.raises(DocumentTextCursorError):
        make_document_text_cursor(
            cache_id="abc", source="csjn", offset=0, limit=maximum + 1
        )


def test_make_rejects_wrong_operation() -> None:
    with pytest.raises(DocumentTextCursorError):
        make_document_text_cursor(
            cache_id="abc", source="csjn", offset=0, limit=5, operation="search"
        )


def test_decode_rejects_malformed_cursor() -> None:
    with pytest.raises(DocumentTextCursorError):
        decode_document_text_cursor("!!!not-base64!!!")
    with pytest.raises(DocumentTextCursorError):
        decode_document_text_cursor("")


def test_decode_checks_cache_presence(tmp_path: Path) -> None:
    cache = DocumentTextCache(base_dir=tmp_path)
    record = cache.put(source="csjn", document_ref={"id": "1"}, text="body")
    good = make_document_text_cursor(
        cache_id=record.cache_id, source="csjn", offset=0, limit=5
    )
    payload = decode_document_text_cursor(good, cache=cache)
    assert payload["cache_id"] == record.cache_id

    missing = make_document_text_cursor(
        cache_id="unknown-id", source="csjn", offset=0, limit=5
    )
    with pytest.raises(DocumentTextCursorError):
        decode_document_text_cursor(missing, cache=cache)
