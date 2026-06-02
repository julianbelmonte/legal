"""Document text resolver subpackage for the MCP server.

The MCP ``legal_get_document_text`` tool exposes one source-agnostic way to
fetch a document's extracted text. Each legal source names its
document-retrieval operation differently (``csjn documento``/``download``,
``saij download``, ``ptn download``, ``sentencias-scba pdf``/``get``,
``infoleg get``, ``pjn-juris download``, ...), and produces text, metadata, and
URLs in source-specific shapes.

This subpackage owns only the **mapping**: it declares, per source, which
existing pipeline operation the MCP tool must dispatch (via
``legal.dispatch.run_operation``) and how to read text/metadata/url/provenance
out of the resulting normalized envelope. It adds **no** source-access logic of
its own — full fetch + extraction wiring lands in later steps (10-13).
"""

from __future__ import annotations

from mcp_server.document_text.resolvers import (
    DocumentTextResolver,
    DocumentTextStrategy,
    TextMode,
    document_text_error,
    get_document_text_resolver,
    supported_document_text_sources,
)

__all__ = [
    "DocumentTextResolver",
    "DocumentTextStrategy",
    "TextMode",
    "document_text_error",
    "get_document_text_resolver",
    "supported_document_text_sources",
]
