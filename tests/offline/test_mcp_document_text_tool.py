"""Unit tests for the MCP initial document-text tool.

``legal_get_document_text`` resolves a document through the resolver registry,
extracts the full text internally, caches it, and returns the first text page
plus paging metadata and cursors. These tests mock the resolver fetch (no
network) and prove: the signature, the unsupported-source error envelope, the
first-page slice with ``has_more``/``next_cursor``, and the complete empty page
for a document with no text.
"""

from __future__ import annotations

from inspect import signature

import pytest

from server.settings import reload_mcp_settings
from server.tools import document_text as tool
from server.tools.document_text import legal_get_document_text


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
    sig = signature(legal_get_document_text)
    for name in ("source_id", "document_id", "params", "page_size_chars"):
        assert name in sig.parameters


def test_unsupported_source_returns_error_envelope() -> None:
    result = legal_get_document_text("not-a-source", "x")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"


def test_first_page_slices_and_sets_next_cursor(monkeypatch) -> None:
    long_text = "A" * 250

    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            assert document_id == "doc-1"
            return _envelope(long_text)

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())

    result = legal_get_document_text("csjn", "doc-1", page_size_chars=100)

    assert result["ok"] is True
    assert result["source"] == "csjn"
    assert result["document"]["id"] == "doc-1"
    assert result["document"]["title"] == "Fallo"
    assert result["text_page"]["text"] == "A" * 100
    assert result["text_page"]["start_char"] == 0
    assert result["text_page"]["end_char"] == 100
    assert result["text_page"]["total_chars"] == 250
    assert result["page"]["limit"] == 100
    assert result["page"]["total"] == 250
    assert result["page"]["has_more"] is True
    assert result["page"]["next_cursor"] is not None
    assert result["page"]["prev_cursor"] is None
    assert result["provenance"] == {"source_urls": ["https://example/doc-1"]}
    assert result["warnings"] == ["w1"]
    assert result["cache_id"]


def test_empty_document_returns_complete_empty_page(monkeypatch) -> None:
    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            return _envelope(None)

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())

    result = legal_get_document_text("csjn", "doc-1")

    assert result["ok"] is True
    assert result["text_page"]["text"] == ""
    assert result["text_page"]["total_chars"] == 0
    assert result["page"]["total"] == 0
    assert result["page"]["has_more"] is False
    assert result["page"]["next_cursor"] is None
    assert result["page"]["prev_cursor"] is None


def test_failed_fetch_passes_through_error(monkeypatch) -> None:
    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            return {
                "ok": False,
                "source": "csjn",
                "operation": "documento",
                "error": {"code": "source_unavailable", "message": "boom"},
            }

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())

    result = legal_get_document_text("csjn", "doc-1")
    assert result["ok"] is False
    assert result["error"]["code"] == "source_unavailable"
