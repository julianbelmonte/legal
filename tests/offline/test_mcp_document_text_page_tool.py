"""Unit tests for the MCP document-text page tool.

``legal_get_document_text_page`` resumes reading a cached document by opaque
cursor, returning the exact requested window under ``document.text_page`` with
``next_cursor``/``prev_cursor`` when applicable. These tests prove: the precise
slice, the document-nested shape, the malformed-cursor usage error, and the
retryable error returned when the cursor references an expired/missing record.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mcp_server.document_text.cache import DocumentTextCache
from mcp_server.document_text.cursors import make_document_text_cursor
from mcp_server.settings import reload_mcp_settings
from mcp_server.tools.document_text import legal_get_document_text_page


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGAL_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LEGAL_MCP_MAX_PAGE_SIZE", "100")
    reload_mcp_settings()
    yield
    reload_mcp_settings()


def test_returns_exact_window_in_document_nested_shape() -> None:
    cache = DocumentTextCache()
    record = cache.put(
        source="test",
        document_ref={"id": "1"},
        text="0123456789",
        metadata={"title": "Doc"},
    )
    cursor = make_document_text_cursor(
        cache_id=record.cache_id, source="test", offset=3, limit=4
    )

    page = legal_get_document_text_page(cursor)

    assert page["ok"] is True
    assert page["source"] == "test"
    text_page = page["document"]["text_page"]
    assert text_page["text"] == "3456"
    assert text_page["start_char"] == 3
    assert text_page["end_char"] == 7
    assert text_page["total_chars"] == 10
    assert page["document"]["title"] == "Doc"
    assert page["page"]["has_more"] is True
    assert page["page"]["next_cursor"] is not None
    assert page["page"]["prev_cursor"] is not None


def test_malformed_cursor_returns_usage_error() -> None:
    result = legal_get_document_text_page("not-a-real-cursor")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"
    assert result["error"]["retryable"] is False


def test_expired_record_returns_retryable_error() -> None:
    cache = DocumentTextCache(ttl=timedelta(seconds=-1))
    record = cache.put(
        source="test",
        document_ref={"id": "1"},
        text="0123456789",
        metadata={},
    )
    # Mint the cursor through a non-expiring cache so encoding succeeds.
    cursor = make_document_text_cursor(
        cache_id=record.cache_id, source="test", offset=0, limit=4
    )

    result = legal_get_document_text_page(cursor)

    assert result["ok"] is False
    assert result["error"]["code"] == "cache_expired"
    assert result["error"]["retryable"] is True
