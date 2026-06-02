"""MCP server application shell for the legal data pipeline.

This module exposes the app shell: the ``create_app`` factory and a
module-level ``app`` for ASGI serving. The MCP server is a sibling consumer to
``api`` that reuses the existing pipeline seams (``legal.registry``,
``legal.dispatch``, ``legal.global_search``, ``legal.schema``,
``legal.pagination``, ``legal.cache``, ``legal.models``) and adds no
source-access logic of its own.

The MCP transport is served over streamable HTTP. ``create_app`` returns a
mountable ASGI app that bundles the OAuth discovery/flow routes (reachable
without a bearer token) and the bearer-protected ``/mcp`` transport, so the same
factory can run standalone or be mounted beside the API (see
``api.main.create_app``). Run the standalone MCP app locally with::

    uv run python -m mcp_server.main
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from mcp_server.auth.routes import build_oauth_routes
from mcp_server.auth.transport import BearerAuthMiddleware
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
    # ``streamable_http_path="/"`` makes the transport's own route ``/`` so the
    # streamable app can be mounted at ``/mcp`` with the endpoint landing exactly
    # at ``/mcp`` (rather than ``/mcp/mcp``).
    server = FastMCP(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        streamable_http_path="/",
    )
    read_only = ToolAnnotations(readOnlyHint=True)
    for tool in _READ_ONLY_TOOLS:
        server.add_tool(tool, annotations=read_only)
    return server


def build_mcp_asgi_app(settings: McpSettings | None = None) -> ASGIApp:
    """Return the bearer-protected MCP streamable-HTTP ASGI app.

    Builds the FastMCP streamable transport (endpoint at ``/`` so it mounts
    cleanly at ``/mcp``) and wraps it in
    :class:`~mcp_server.auth.transport.BearerAuthMiddleware`. The middleware
    only challenges requests under the configured MCP path, so when the inner
    app is mounted at ``/mcp`` the guard protects ``/mcp`` while leaving the
    health and OAuth surfaces open. The streamable app carries its own lifespan
    (the session manager), which the mounting parent runs.
    """
    settings = settings or load_settings()
    server = build_mcp_server(settings)
    return BearerAuthMiddleware(server.streamable_http_app(), settings=settings)


def create_app(settings: McpSettings | None = None) -> Starlette:
    """Build and return the standalone MCP ASGI application.

    The returned Starlette app bundles:

    * an unauthenticated ``/healthz`` liveness probe;
    * the OAuth discovery + flow routes (``/.well-known/*`` and ``/oauth/*``),
      reachable without a bearer token;
    * the bearer-protected MCP streamable transport mounted at ``/mcp``.

    The same app can run standalone under uvicorn or be composed beside the API
    (the API's ``create_app`` mounts the MCP transport and registers the OAuth
    routes through the same building blocks).
    """
    settings = settings or load_settings()
    mcp_app = build_mcp_asgi_app(settings)

    async def healthz(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    routes = [Route("/healthz", healthz, methods=["GET"])]
    routes.extend(build_oauth_routes(settings=settings))
    routes.append(Mount("/mcp", app=mcp_app))
    return Starlette(routes=routes)


app = create_app()


def main() -> None:
    """Module entry point for local development.

    Runs the standalone MCP ASGI app under uvicorn. Bind host/port are read from
    ``LEGAL_MCP_HOST`` / ``LEGAL_MCP_PORT`` (falling back to ``127.0.0.1:8081``)
    so it does not collide with the API's default port.
    """
    import os

    import uvicorn

    host = os.environ.get("LEGAL_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("LEGAL_MCP_PORT", "8081"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover - local dev entry point
    main()
