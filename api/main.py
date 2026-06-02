"""FastAPI application for the legal data pipeline.

This module exposes the app shell: the ``create_app`` factory, an
unauthenticated ``/healthz`` endpoint, and a module-level ``app`` for uvicorn.

A single ASGI app serves three surfaces behind one uvicorn process:

* the existing API routes (``/v1`` with ``x-api-key`` auth) plus ``/healthz``,
  unchanged;
* the OAuth discovery/flow endpoints (``/.well-known/*`` and ``/oauth/*``),
  reachable without a bearer token, wired to the single-user OAuth provider;
* the MCP streamable-HTTP transport mounted at ``/mcp``, bearer-protected so
  unauthenticated calls get ``401`` + ``WWW-Authenticate``.

Run locally with::

    uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from legal.errors import LegalCliError

from api.errors import error_to_envelope
from api.routers import csjn, discovery, generic, saij, search
from mcp_server.auth.routes import build_oauth_routes
from mcp_server.main import build_mcp_asgi_app

DESCRIPTION = (
    "Uniform HTTP access to Argentina legal research data sources. Every "
    "source is an abstract entity exposed through source-agnostic operations "
    "(search, get, download, ...), with typed ad-hoc endpoints for the most "
    "relevant sources. Responses echo the pipeline's normalized JSON envelope "
    "1:1 with the CLI."
)


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Legal Data API",
        version="0.1.0",
        description=DESCRIPTION,
    )

    @app.exception_handler(LegalCliError)
    async def legal_cli_error_handler(request: Request, exc: LegalCliError) -> JSONResponse:
        """Convert a raised ``LegalCliError`` into the normalized error envelope.

        Source/operation context is taken from the matched path params when
        available so the envelope mirrors the CLI's error output.
        """
        params = request.path_params or {}
        source = params.get("source_id") or params.get("source") or "unknown"
        operation = params.get("operation") or params.get("op") or "unknown"
        env, status = error_to_envelope(exc, source=source, operation=operation)
        return JSONResponse(content=env, status_code=status)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        """Unauthenticated liveness probe."""
        return {"ok": True, "service": "legal-api"}

    app.include_router(discovery.router)
    app.include_router(generic.router)
    app.include_router(search.router)
    app.include_router(csjn.router)
    app.include_router(saij.router)

    # OAuth discovery + flow endpoints, reachable without a bearer token. These
    # are Starlette routes (``/.well-known/*`` and ``/oauth/*``) added directly
    # so the API and the MCP transport advertise a single OAuth surface.
    app.router.routes.extend(build_oauth_routes())

    # MCP streamable-HTTP transport, bearer-protected by the wrapping
    # middleware. Mounting at ``/mcp`` lands the transport's root endpoint
    # exactly at ``/mcp``; Starlette runs the mounted app's lifespan (the MCP
    # session manager) alongside the API's.
    app.mount("/mcp", build_mcp_asgi_app())

    return app


app = create_app()
