"""Initial document-text retrieval tool for the Argentina legal research MCP surface.

``legal_get_document_text`` is the source-agnostic entry point for reading a
single document's text over MCP. It wires together the prior building blocks and
adds no source-access logic of its own:

- :mod:`mcp_server.document_text.resolvers` maps a source id onto the existing
  pipeline operation that fetches the document (dispatched through
  ``legal.dispatch.run_operation``) and declares how text/metadata/url are read
  out of the normalized envelope.
- :mod:`mcp_server.document_text.cache` stores the full extracted text plus its
  metadata under a TTL record, so subsequent page reads never refetch or
  re-extract a large document.
- :mod:`mcp_server.document_text.cursors` mints the opaque cursors that resume
  reading at a character offset.

The MCP surface never exposes raw PDF bytes or filesystem save paths. The
resolver requests text-only output; this tool returns a deliberate text *slice*
(the first page) plus the page metadata and cursors needed to read the rest.
Text is **never** silently truncated -- a document longer than one page reports
``has_more`` with a ``next_cursor``, and a document with no text returns a
complete empty page (``total_chars`` 0, ``has_more`` False, ``next_cursor``
None).
"""

from __future__ import annotations

from typing import Any, Mapping

from legal.cache import query_hash as _query_hash
from mcp_server.document_text.cache import DocumentTextCache, DocumentTextRecord
from mcp_server.document_text.cursors import (
    DocumentTextCursorError,
    decode_document_text_cursor,
    make_document_text_cursor,
)
from mcp_server.document_text.resolvers import (
    document_text_error,
    get_document_text_resolver,
)
from mcp_server.serialization import SerializationError, error_envelope, to_jsonable
from mcp_server.settings import get_mcp_settings

__all__ = [
    "DOCUMENT_TEXT_TOOL_OPERATION",
    "DOCUMENT_TEXT_PAGE_TOOL_OPERATION",
    "legal_get_document_text",
    "legal_get_document_text_page",
    "build_text_page",
]

# Operation tag carried on the returned envelope so an MCP client (and the
# follow-up page/find tools) can recognize a document-text result.
DOCUMENT_TEXT_TOOL_OPERATION = "get_document_text"

# Operation tag for cursor-driven page reads.
DOCUMENT_TEXT_PAGE_TOOL_OPERATION = "get_document_text_page"


def _resolve_page_size(page_size_chars: int | None) -> int:
    """Clamp the requested page size into ``(0, max_page_size]``.

    Defaults to and is capped at the MCP ``max_page_size`` setting so a single
    page can never exceed the configured per-page character budget.
    """
    maximum = get_mcp_settings().max_page_size
    if page_size_chars is None:
        return maximum
    try:
        size = int(page_size_chars)
    except (TypeError, ValueError):
        return maximum
    if size <= 0:
        return maximum
    return min(size, maximum)


def _extract_text(document: Mapping[str, Any]) -> str:
    """Read the document's full extracted text out of a serialized document.

    The pipeline surfaces extracted/direct text in ``body`` for every text-
    bearing source; some PDF-backed operations also mirror it under
    ``metadata.text``. Prefer the document body, fall back to the metadata text,
    and normalize a missing/blank value to the empty string.
    """
    body = document.get("body")
    if isinstance(body, str) and body:
        return body
    metadata = document.get("metadata")
    if isinstance(metadata, Mapping):
        meta_text = metadata.get("text")
        if isinstance(meta_text, str) and meta_text:
            return meta_text
    return ""


def build_text_page(
    *,
    cache_id: str,
    source: str,
    text: str,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Build the text-page payload for a slice of a cached document's text.

    Returns a deliberate slice ``text[offset:offset+limit]`` plus the page
    metadata (``start_char``/``end_char``/``total_chars``) and paging metadata
    (``limit``/``offset``/``total``/``has_more``/``next_cursor``/``prev_cursor``).
    Slicing is explicit, never silent truncation: ``has_more`` and a
    ``next_cursor`` are returned whenever text remains past ``end_char``.

    An empty document yields a complete empty page: empty text, ``total`` 0,
    ``has_more`` False, and ``next_cursor`` None.
    """
    total = len(text)
    start = max(0, offset)
    end = min(total, start + limit)
    chunk = text[start:end] if start < end else ""

    has_more = end < total
    next_cursor = (
        make_document_text_cursor(
            cache_id=cache_id, source=source, offset=end, limit=limit
        )
        if has_more
        else None
    )
    prev_offset = start - limit
    prev_cursor = (
        make_document_text_cursor(
            cache_id=cache_id,
            source=source,
            offset=max(0, prev_offset),
            limit=limit,
        )
        if start > 0
        else None
    )

    return {
        "cache_id": cache_id,
        "text_page": {
            "text": chunk,
            "start_char": start,
            "end_char": end,
            "total_chars": total,
        },
        "page": {
            "limit": limit,
            "offset": start,
            "total": total,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
        },
    }


def _document_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    """Pick the client-facing document identity fields from a serialized doc.

    Surfaces id/title/date/url/file_url when present. Never exposes raw bytes,
    save paths, or attachment binary -- only descriptive metadata.
    """
    fields = ("id", "title", "date", "url", "file_url")
    return {key: document[key] for key in fields if document.get(key) is not None}


def legal_get_document_text(
    source_id: str,
    document_id: str,
    params: Mapping[str, Any] | None = None,
    page_size_chars: int | None = None,
) -> dict[str, Any]:
    """Fetch a legal document's text and return its first page over MCP.

    Resolves ``source_id`` to its document-text strategy, dispatches the mapped
    existing pipeline operation (via ``legal.dispatch.run_operation``) to fetch
    the document with ``document_id`` (plus any source-specific ``params``
    overrides), extracts the full text internally, caches the text + metadata
    under a TTL record, and returns the first text page.

    The returned envelope carries:

    - ``ok``/``source``/``operation`` plus the resolved ``document`` identity
      (id/title/date/url/file_url where available);
    - ``cache_id`` and a ``text_page`` (``text``/``start_char``/``end_char``/
      ``total_chars``);
    - a ``page`` block (``limit``/``offset``/``total``/``has_more``/
      ``next_cursor``/``prev_cursor``);
    - the source ``provenance`` and any ``warnings``.

    An unsupported source returns the normalized ``usage_error`` envelope. A
    document with no extractable text returns a complete empty page (``total`` 0,
    ``has_more`` False, ``next_cursor`` None). Raw PDF bytes and filesystem save
    paths are never exposed.
    """
    resolver = get_document_text_resolver(source_id)
    if resolver is None:
        return to_jsonable(document_text_error(source_id))

    page_size = _resolve_page_size(page_size_chars)

    result = resolver.fetch(document_id, overrides=params)

    try:
        envelope = to_jsonable(result)
    except SerializationError as exc:
        return error_envelope(
            source=source_id,
            operation=DOCUMENT_TEXT_TOOL_OPERATION,
            message=str(exc),
        )

    # A failed fetch already carries the pipeline's normalized error envelope;
    # surface it unchanged rather than caching an empty document.
    if not envelope.get("ok", False) or envelope.get("error") is not None:
        return envelope

    document = envelope.get("document")
    if not isinstance(document, Mapping):
        return error_envelope(
            source=source_id,
            operation=DOCUMENT_TEXT_TOOL_OPERATION,
            message=(
                f"source '{source_id}' returned no document for id {document_id!r}"
            ),
            code="source_unavailable",
        )

    text = _extract_text(document)
    provenance = envelope.get("provenance")
    warnings = envelope.get("warnings") or []
    metadata = _document_metadata(document)

    document_ref = {"source": source_id, "document_id": document_id}
    if params:
        document_ref["params"] = dict(params)

    cache = DocumentTextCache()
    record: DocumentTextRecord = cache.put(
        source=source_id,
        document_ref=document_ref,
        text=text,
        metadata=metadata,
        query_hash=_query_hash(document_ref),
        title=metadata.get("title"),
        date=metadata.get("date"),
        url=metadata.get("url"),
        provenance=provenance if isinstance(provenance, Mapping) else None,
    )

    page = build_text_page(
        cache_id=record.cache_id,
        source=source_id,
        text=text,
        offset=0,
        limit=page_size,
    )

    return {
        "ok": True,
        "source": source_id,
        "operation": DOCUMENT_TEXT_TOOL_OPERATION,
        "document": metadata,
        "cache_id": record.cache_id,
        "text_page": page["text_page"],
        "page": page["page"],
        "provenance": provenance,
        "warnings": warnings,
    }


def legal_get_document_text_page(cursor: str) -> dict[str, Any]:
    """Return the exact text page referenced by an opaque document-text cursor.

    Decodes and validates ``cursor`` (rejecting a wrong operation, negative
    offset, out-of-range limit, or malformed payload), loads the cached
    :class:`DocumentTextRecord` it points at, and returns the precise requested
    window of that record's text. The page never silently truncates: it carries
    ``next_cursor``/``prev_cursor`` whenever more text exists before or after the
    returned window.

    A malformed or invalid cursor returns the normalized ``usage_error``
    envelope. When the cursor references a cache record that is missing or
    expired, a normalized **retryable** error envelope is returned so the client
    knows to re-fetch the document and obtain a fresh cursor. Raw PDF bytes and
    filesystem save paths are never exposed.

    The returned page nests the slice under ``document``: the payload exposes
    ``document.text_page`` (``text``/``start_char``/``end_char``/``total_chars``)
    alongside the page identity and the ``page`` paging block.
    """
    cache = DocumentTextCache()

    try:
        payload = decode_document_text_cursor(cursor)
    except DocumentTextCursorError as exc:
        return error_envelope(
            source="document_text",
            operation=DOCUMENT_TEXT_PAGE_TOOL_OPERATION,
            message=str(exc),
            code="usage_error",
        )

    cache_id = payload["cache_id"]
    source = payload["source"]
    offset = payload["offset"]
    limit = payload["limit"]

    record = cache.get(cache_id)
    if record is None:
        return error_envelope(
            source=source,
            operation=DOCUMENT_TEXT_PAGE_TOOL_OPERATION,
            message=(
                f"document text cache id {cache_id!r} is unknown or expired; "
                "re-fetch the document to obtain a fresh cursor"
            ),
            code="cache_expired",
            retryable=True,
        )

    page = build_text_page(
        cache_id=cache_id,
        source=source,
        text=record.text,
        offset=offset,
        limit=limit,
    )

    return {
        "ok": True,
        "source": source,
        "operation": DOCUMENT_TEXT_PAGE_TOOL_OPERATION,
        "document": {
            **record.metadata,
            "text_page": page["text_page"],
        },
        "cache_id": cache_id,
        "page": page["page"],
        "provenance": record.provenance or None,
        "warnings": [],
    }
