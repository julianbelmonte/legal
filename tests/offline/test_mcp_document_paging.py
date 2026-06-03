"""Document text paging tests.

These tests prove that MCP document text is *paged*, *navigable* via cursors,
and **never silently truncated**. Every test node id contains both "document"
and "paging" (the file path ``test_mcp_document_paging.py`` is part of the node
id, and every test function is named ``test_document_paging_*``), so the
acceptance selector ``-k "document and paging"`` collects them.

The resolver fetch is mocked (no network) and the cache is isolated to a tmp
dir via ``LEGAL_CACHE_DIR``. Across the suite we assert two invariants on every
returned page:

* the returned page text length never exceeds the requested page size, and
* the response carries the total character count
  (``text_page.total_chars`` and ``page.total``).

Several tests reconstruct the full document by concatenating every page (walking
``next_cursor``) and assert the result equals the original text, proving no
silent truncation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from server.document_text.cache import DocumentTextCache
from server.document_text.cursors import make_document_text_cursor
from server.settings import reload_mcp_settings
from server.tools import document_text as tool
from server.tools.document_text import (
    legal_find_in_document_text,
    legal_get_document_text,
    legal_get_document_text_page,
)

PAGE_SIZE = 100


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    """Isolate the document-text cache and pin a small max page size."""
    monkeypatch.setenv("LEGAL_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("LEGAL_MCP_MAX_PAGE_SIZE", str(PAGE_SIZE))
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


def _install_resolver(monkeypatch, body: str | None) -> None:
    """Mock the resolver registry so a fetch returns ``body`` with no network."""

    class FakeResolver:
        def fetch(self, document_id, *, overrides=None, raw=False):
            assert document_id == "doc-1"
            return _envelope(body)

    monkeypatch.setattr(tool, "get_document_text_resolver", lambda s: FakeResolver())


def _text_page_of(result: dict) -> dict:
    """Read ``text_page`` from either tool shape.

    ``legal_get_document_text`` exposes ``text_page`` at the top level;
    ``legal_get_document_text_page`` nests it under ``document``.
    """
    if "text_page" in result:
        return result["text_page"]
    return result["document"]["text_page"]


def _assert_page_invariants(result: dict, *, requested_size: int) -> dict:
    """Assert the never-truncate invariants and return the text page."""
    assert result["ok"] is True
    text_page = _text_page_of(result)
    page = result["page"]

    # Page text never exceeds the requested page size.
    assert len(text_page["text"]) <= requested_size
    # Total character counts are always reported, in both blocks, in agreement.
    assert text_page["total_chars"] == page["total"]
    # The slice window is internally consistent.
    assert text_page["end_char"] - text_page["start_char"] == len(text_page["text"])
    return text_page


def _walk_all_pages(first: dict, *, requested_size: int) -> str:
    """Follow ``next_cursor`` from a first page and concatenate every slice.

    Asserts the page invariants on every page and that ``next_cursor`` is None
    exactly when ``has_more`` is False (last page).
    """
    text_page = _assert_page_invariants(first, requested_size=requested_size)
    collected = text_page["text"]
    page = first["page"]

    guard = 0
    while page["has_more"]:
        guard += 1
        assert guard < 10_000, "paging did not terminate"
        assert page["next_cursor"] is not None
        nxt = legal_get_document_text_page(page["next_cursor"])
        text_page = _assert_page_invariants(nxt, requested_size=requested_size)
        collected += text_page["text"]
        page = nxt["page"]

    # On the last page there is no next cursor.
    assert page["has_more"] is False
    assert page["next_cursor"] is None
    return collected


def test_document_paging_first_page_sets_next_cursor(monkeypatch) -> None:
    long_text = "A" * 250
    _install_resolver(monkeypatch, long_text)

    result = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)

    text_page = _assert_page_invariants(result, requested_size=PAGE_SIZE)
    assert text_page["text"] == "A" * PAGE_SIZE
    assert text_page["start_char"] == 0
    assert text_page["end_char"] == PAGE_SIZE
    assert text_page["total_chars"] == 250
    assert result["page"]["has_more"] is True
    assert result["page"]["next_cursor"] is not None
    assert result["page"]["prev_cursor"] is None


def test_document_paging_next_page_follows_cursor(monkeypatch) -> None:
    long_text = "".join(chr(ord("a") + (i % 26)) for i in range(250))
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    _assert_page_invariants(first, requested_size=PAGE_SIZE)

    second = legal_get_document_text_page(first["page"]["next_cursor"])
    text_page = _assert_page_invariants(second, requested_size=PAGE_SIZE)

    assert text_page["start_char"] == PAGE_SIZE
    assert text_page["end_char"] == 2 * PAGE_SIZE
    assert text_page["text"] == long_text[PAGE_SIZE : 2 * PAGE_SIZE]
    # Page tool nests identity + text_page under "document".
    assert second["document"]["id"] == "doc-1"
    assert second["page"]["prev_cursor"] is not None


def test_document_paging_previous_page_follows_prev_cursor(monkeypatch) -> None:
    long_text = "".join(chr(ord("a") + (i % 26)) for i in range(250))
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    second = legal_get_document_text_page(first["page"]["next_cursor"])
    assert second["page"]["prev_cursor"] is not None

    back = legal_get_document_text_page(second["page"]["prev_cursor"])
    text_page = _assert_page_invariants(back, requested_size=PAGE_SIZE)

    # Stepping back from page 2 lands exactly on page 1's window.
    assert text_page["start_char"] == 0
    assert text_page["end_char"] == PAGE_SIZE
    assert text_page["text"] == long_text[0:PAGE_SIZE]
    assert back["page"]["prev_cursor"] is None


def test_document_paging_reconstructs_full_text_without_truncation(monkeypatch) -> None:
    # 250 chars over a 100-char page -> 3 pages (100 + 100 + 50).
    long_text = "".join(str(i % 10) for i in range(250))
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    reconstructed = _walk_all_pages(first, requested_size=PAGE_SIZE)

    assert reconstructed == long_text
    assert len(reconstructed) == 250


def test_document_paging_last_partial_page_boundary(monkeypatch) -> None:
    long_text = "Z" * 250  # last page holds 50 chars.
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    page2 = legal_get_document_text_page(first["page"]["next_cursor"])
    page3 = legal_get_document_text_page(page2["page"]["next_cursor"])

    text_page = _assert_page_invariants(page3, requested_size=PAGE_SIZE)
    assert text_page["start_char"] == 200
    assert text_page["end_char"] == 250
    assert len(text_page["text"]) == 50
    # Last page: has_more flips False and next_cursor is None.
    assert page3["page"]["has_more"] is False
    assert page3["page"]["next_cursor"] is None


def test_document_paging_exact_multiple_has_more_flips_false(monkeypatch) -> None:
    # Exactly two full pages: the boundary must not invent an empty extra page.
    long_text = "Q" * (2 * PAGE_SIZE)
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    assert first["page"]["has_more"] is True

    page2 = legal_get_document_text_page(first["page"]["next_cursor"])
    text_page = _assert_page_invariants(page2, requested_size=PAGE_SIZE)
    assert text_page["start_char"] == PAGE_SIZE
    assert text_page["end_char"] == 2 * PAGE_SIZE
    assert page2["page"]["has_more"] is False
    assert page2["page"]["next_cursor"] is None


def test_document_paging_offset_at_end_returns_empty_window(monkeypatch) -> None:
    long_text = "Y" * 250
    _install_resolver(monkeypatch, long_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    cache_id = first["cache_id"]

    # A cursor pointing exactly at the end of the text is a complete, empty page.
    end_cursor = make_document_text_cursor(
        cache_id=cache_id, source="csjn", offset=250, limit=PAGE_SIZE
    )
    result = legal_get_document_text_page(end_cursor)
    text_page = _assert_page_invariants(result, requested_size=PAGE_SIZE)

    assert text_page["text"] == ""
    assert text_page["start_char"] == 250
    assert text_page["end_char"] == 250
    assert text_page["total_chars"] == 250
    assert result["page"]["has_more"] is False
    assert result["page"]["next_cursor"] is None
    # Text remains behind us, so we can still page backward.
    assert result["page"]["prev_cursor"] is not None


def test_document_paging_empty_document_complete_empty_page(monkeypatch) -> None:
    _install_resolver(monkeypatch, None)

    result = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    text_page = _assert_page_invariants(result, requested_size=PAGE_SIZE)

    assert text_page["text"] == ""
    assert text_page["total_chars"] == 0
    assert result["page"]["total"] == 0
    assert result["page"]["has_more"] is False
    assert result["page"]["next_cursor"] is None
    assert result["page"]["prev_cursor"] is None


def test_document_paging_unicode_paged_by_character_offset(monkeypatch) -> None:
    # Multi-byte chars: paging must be by CHARACTER offset, not byte offset.
    # Each grapheme here is a single Python str char but multiple UTF-8 bytes.
    unicode_text = ("ñé€✓你好" * 60)  # 360 characters, all multi-byte.
    assert len(unicode_text) == 360
    assert len(unicode_text.encode("utf-8")) > 360  # genuinely multi-byte.
    _install_resolver(monkeypatch, unicode_text)

    first = legal_get_document_text("csjn", "doc-1", page_size_chars=PAGE_SIZE)
    text_page = _assert_page_invariants(first, requested_size=PAGE_SIZE)

    # First page is exactly the first 100 CHARACTERS, not bytes.
    assert text_page["text"] == unicode_text[:PAGE_SIZE]
    assert text_page["total_chars"] == 360

    reconstructed = _walk_all_pages(first, requested_size=PAGE_SIZE)
    assert reconstructed == unicode_text
    # No character was split or dropped across page boundaries.
    assert len(reconstructed) == 360


def test_document_paging_invalid_cursor_returns_usage_error() -> None:
    result = legal_get_document_text_page("not-a-real-cursor")
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"


def test_document_paging_malformed_cursor_payload_usage_error() -> None:
    # A wrong-operation cursor cannot be minted via make_document_text_cursor,
    # but a base64url cursor from elsewhere must still be rejected as usage_error.
    from legal.pagination import encode_cursor

    foreign = encode_cursor(
        {"source": "csjn", "operation": "search", "offset": 0, "limit": 10}
    )
    result = legal_get_document_text_page(foreign)
    assert result["ok"] is False
    assert result["error"]["code"] == "usage_error"


def test_document_paging_expired_cache_returns_retryable_error() -> None:
    # Write a record that is already expired (negative TTL), then page it.
    cache = DocumentTextCache(ttl=timedelta(seconds=-1))
    record = cache.put(
        source="csjn",
        document_ref={"source": "csjn", "document_id": "doc-1"},
        text="Z" * 250,
        metadata={"id": "doc-1", "title": "Fallo"},
    )
    cursor = make_document_text_cursor(
        cache_id=record.cache_id, source="csjn", offset=0, limit=PAGE_SIZE
    )

    result = legal_get_document_text_page(cursor)
    assert result["ok"] is False
    assert result["error"]["code"] == "cache_expired"
    assert result["error"].get("retryable") is True


def test_document_paging_find_returns_match_cursors_opening_windows(monkeypatch) -> None:
    # Place two matches: one near the start, one deep in a later page, so the
    # returned cursors must open windows around DIFFERENT offsets.
    prefix = "a" * 20
    gap = "b" * 200
    text = prefix + "NEEDLE" + gap + "NEEDLE" + ("c" * 20)
    _install_resolver(monkeypatch, text)

    result = legal_find_in_document_text(
        "needle", source_id="csjn", document_id="doc-1", page_size_chars=PAGE_SIZE
    )

    assert result["ok"] is True
    assert result["match_count"] == 2
    assert result["total_chars"] == len(text)
    items = result["items"]
    assert len(items) == 2

    first_match, second_match = items
    assert text[first_match["start_char"] : first_match["end_char"]] == "NEEDLE"
    assert text[second_match["start_char"] : second_match["end_char"]] == "NEEDLE"

    # Each match cursor opens a page window AROUND its match.
    for match in items:
        page = legal_get_document_text_page(match["cursor"])
        text_page = _assert_page_invariants(page, requested_size=PAGE_SIZE)
        window_start = text_page["start_char"]
        window_end = text_page["end_char"]
        # The match falls inside the window the cursor opened.
        assert window_start <= match["start_char"]
        assert match["end_char"] <= window_end

    # The two windows are centered on different parts of the document.
    page1 = legal_get_document_text_page(items[0]["cursor"])
    page2 = legal_get_document_text_page(items[1]["cursor"])
    assert _text_page_of(page1)["start_char"] != _text_page_of(page2)["start_char"]
