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

from mcp_server.settings import McpSettings, load_settings


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
