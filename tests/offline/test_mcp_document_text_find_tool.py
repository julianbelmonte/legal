"""Unit tests for the MCP search-within-document tool.

``legal_find_in_document_text`` finds every case-insensitive occurrence of a
query inside a document's cached text. It accepts either an opaque document-text
cursor (loads the cached text directly, no refetch) or a document reference
(``source_id`` + ``document_id``, which is resolved and cached first, reusing the
``legal_get_document_text`` path). These tests mock the resolver fetch (no
network, no credentials) and prove: the signature, the document-reference path
with matches and per-match cursors, the cursor path that reuses the cache without
refetching, the empty-query usage error, the missing-reference usage error, the
malformed-cursor usage error, and the retryable error when the cursor references
an expired/missing cache record.
"""

from __future__ import annotations

from datetime import timedelta
from inspect import signature

import pytest

from server.document_text.cache import DocumentTextCache
from server.document_text.cursors import make_document_text_cursor
from server.settings import reload_mcp_settings
from server.tools import document_text as tool
from server.tools.document_text import (
    DOCUMENT_TEXT_FIND_TOOL_OPERATION,
    legal_find_in_document_text,
)


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGAL_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LEGAL_MCP_MAX_PAGE_SIZE", "100")
    reload_mcp_settings()
    yield
    reload_mcp_settings()


def _envelope(body: str | None) -> dict:
    return {
        "ok": True,
        "source": "csjn",
        "operation": "documento",
        "document": {
            "id": "doc-1",
            "title": "Fallo",
            "date": "2024-01-01",
            "url": "https://example/doc-1",
            "file_url": "https://example/doc-1.pdf",
            "body": body,
        },
        "provenance": {"source_urls": ["https://example/doc-1"]},
        "warnings": ["w1"],
    }


def test_signature_exposes_parameters() -> None:
    sig = signature(legal_find_in_document_text)
    for name in (
        "query",
        "cursor",
        "source_id",
        "document_id",
        "params",
        "page_size_chars",
    ):
        assert name in sig.parameters
    assert sig.parameters["cursor"].default is None
    assert sig.parameters["source_id"].default is None
    assert sig.parameters["document_id"].default is None


def test_empty_query_returns_usage_error() -> None:
    result = legal_find_in_document_text("", source_id="csjn", document_id="doc-1")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"
    assert result["operation"] == DOCUMENT_TEXT_FIND_TOOL_OPERATION


def test_missing_reference_returns_usage_error() -> None:
    # Neither a cursor nor a complete source_id + document_id pair is provided.
    result = legal_find_in_document_text("amparo")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"


def test_malformed_cursor_returns_usage_error() -> None:
    result = legal_find_in_document_text("amparo", cursor="not-a-real-cursor")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"
    assert result["error"]["retryable"] is False


def test_document_reference_path_finds_all_matches(monkeypatch) -> None:
    text = "amparo uno. AMPARO dos. nada. amparo tres."

    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            assert document_id == "doc-1"
            return _envelope(text)

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())

    result = legal_find_in_document_text(
        "amparo", source_id="csjn", document_id="doc-1"
    )

    assert result["ok"] is True
    assert result["source"] == "csjn"
    assert result["operation"] == DOCUMENT_TEXT_FIND_TOOL_OPERATION
    assert result["query"] == "amparo"
    # Case-insensitive: all three occurrences are matched.
    assert result["match_count"] == 3
    assert len(result["items"]) == 3
    assert result["total_chars"] == len(text)
    assert result["document"]["id"] == "doc-1"
    assert result["provenance"] == {"source_urls": ["https://example/doc-1"]}
    assert result["warnings"] == ["w1"]
    assert result["cache_id"]
    for item in result["items"]:
        assert text[item["start_char"] : item["end_char"]].lower() == "amparo"
        assert "amparo" in item["snippet"].lower()
        assert item["cursor"]


def test_no_match_returns_empty_items(monkeypatch) -> None:
    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            return _envelope("nothing relevant here")

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())

    result = legal_find_in_document_text(
        "amparo", source_id="csjn", document_id="doc-1"
    )

    assert result["ok"] is True
    assert result["match_count"] == 0
    assert result["items"] == []


def test_cursor_path_uses_cache_without_refetch(monkeypatch) -> None:
    cache = DocumentTextCache()
    record = cache.put(
        source="test",
        document_ref={"id": "1"},
        text="hola amparo mundo",
        metadata={"title": "Doc"},
    )
    cursor = make_document_text_cursor(
        cache_id=record.cache_id, source="test", offset=0, limit=4
    )

    def fail_resolver(_source_id):  # pragma: no cover - must not be called
        raise AssertionError("cursor path must not resolve/refetch the document")

    monkeypatch.setattr(tool, "get_document_text_resolver", fail_resolver)

    result = legal_find_in_document_text("amparo", cursor=cursor)

    assert result["ok"] is True
    assert result["source"] == "test"
    assert result["cache_id"] == record.cache_id
    assert result["match_count"] == 1
    assert result["document"]["title"] == "Doc"


def test_cursor_to_expired_record_returns_retryable_error() -> None:
    cache = DocumentTextCache(ttl=timedelta(seconds=-1))
    record = cache.put(
        source="test",
        document_ref={"id": "1"},
        text="hola amparo mundo",
        metadata={},
    )
    cursor = make_document_text_cursor(
        cache_id=record.cache_id, source="test", offset=0, limit=4
    )

    result = legal_find_in_document_text("amparo", cursor=cursor)

    assert result["ok"] is False
    assert result["error"]["code"] == "cache_expired"
    assert result["error"]["retryable"] is True


def test_unsupported_source_returns_error_envelope() -> None:
    result = legal_find_in_document_text(
        "amparo", source_id="not-a-source", document_id="x"
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"
