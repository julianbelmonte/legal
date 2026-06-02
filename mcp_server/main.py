"""MCP server application shell for the legal data pipeline.

This module exposes the app shell: the ``create_app`` factory and a
module-level ``app`` for ASGI serving. The MCP server is a sibling consumer to
``api`` that reuses the existing pipeline seams (``legal.registry``,
``legal.dispatch``, ``legal.global_search``, ``legal.schema``,
``legal.pagination``, ``legal.cache``, ``legal.models``) and adds no
source-access logic of its own.

Tool registration, OAuth protection, and ASGI mounting beside the API attach in
later steps. Run locally for development with::

    uv run python -m mcp_server.main
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_server.settings import McpSettings, load_settings
from mcp_server.tools import (
    legal_find_in_document_text,
    legal_get_document_text,
    legal_get_document_text_page,
    legal_run_operation,
    legal_schema,
    legal_search,
    legal_source,
    legal_sources,
)

SERVER_NAME = "legal-ar"

SERVER_INSTRUCTIONS = (
    "Use these tools for Argentina (Argentine) legal research: national and "
    "provincial law, jurisprudence, statutes, regulations, and official "
    "gazettes. They cover the Corte Suprema de Justicia de la Nacion (CSJN), "
    "SAIJ, Infoleg, the Boletin Oficial (boletines) at national and provincial "
    "levels, and other provincial legal sources. Reach for them to find fallos "
    "and sumarios, search statutes and regulations, and retrieve full document "
    "text (including paginated reads and in-document search). All tools are "
    "read-only: they only query and return normalized JSON envelopes and never "
    "mutate any source. Start with legal_sources / legal_source / legal_schema "
    "to discover what is wired, use legal_search for a global cross-source "
    "query, legal_run_operation for any specific source/operation pair, and the "
    "legal_get_document_text family to read retrieved documents."
)

# The 8-tool compact surface. Every tool only queries Argentine legal sources
# and returns normalized data, so each is marked read-only for agents.
_READ_ONLY_TOOLS = (
    legal_sources,
    legal_source,
    legal_schema,
    legal_search,
    legal_run_operation,
    legal_get_document_text,
    legal_get_document_text_page,
    legal_find_in_document_text,
)


def build_mcp_server(settings: McpSettings | None = None) -> FastMCP:
    """Construct the MCP server and register the read-only tool surface.

    Builds a :class:`~mcp.server.fastmcp.FastMCP` server with strong
    ``instructions`` guiding agents toward Argentina legal research, and
    registers the eight compact tools with a read-only annotation. Constructing
    the server binds no network port, so this is safe to call at import time and
    from tests.
    """
    _ = settings or load_settings()
    server = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    read_only = ToolAnnotations(readOnlyHint=True)
    for tool in _READ_ONLY_TOOLS:
        server.add_tool(tool, annotations=read_only)
    return server


def create_app(settings: McpSettings | None = None):
    """Build and return the MCP server application.

    Stub: later steps construct the MCP server, register the compact tool
    surface, attach OAuth metadata/protection, and return a mountable ASGI app.
    Returning ``None`` keeps the skeleton importable without network services.
    """
    _ = settings or load_settings()
    return None


app = create_app()


def main() -> None:
    """Module entry point for local development.

    Stub: later steps run the MCP ASGI app under a local server. For now this
    only confirms the app shell is importable and constructable.
    """
    create_app()


if __name__ == "__main__":  # pragma: no cover - local dev entry point
    main()
