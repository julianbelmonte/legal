"""MCP tool surface for the Argentina legal research pipeline.

This package holds the compact MCP tool surface (``legal_sources``,
``legal_source``, ``legal_schema``, ``legal_search``, ``legal_run_operation``,
``legal_get_document_text``, ``legal_get_document_text_page``,
``legal_find_in_document_text``). Each tool is a sibling consumer that reuses
the existing pipeline seams (``legal.registry``, ``legal.schema``,
``legal.dispatch``, ...) and adds no source-access logic of its own.

Tools are grouped by concern into submodules. Discovery tools live in
:mod:`mcp_server.tools.discovery` and the global search tool lives in
:mod:`mcp_server.tools.search`; they are re-exported here so callers can import
them from either the package or the submodule.
"""

from __future__ import annotations

from mcp_server.tools.discovery import (
    legal_schema,
    legal_source,
    legal_sources,
)
from mcp_server.tools.document_text import (
    legal_find_in_document_text,
    legal_get_document_text,
    legal_get_document_text_page,
)
from mcp_server.tools.generic import legal_run_operation
from mcp_server.tools.search import legal_search

__all__ = [
    "legal_sources",
    "legal_source",
    "legal_schema",
    "legal_search",
    "legal_run_operation",
    "legal_get_document_text",
    "legal_get_document_text_page",
    "legal_find_in_document_text",
]
