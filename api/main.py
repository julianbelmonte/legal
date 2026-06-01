"""FastAPI application for the legal data pipeline.

This module exposes the app shell: the ``create_app`` factory, an
unauthenticated ``/healthz`` endpoint, and a module-level ``app`` for uvicorn.
Routers and authentication attach in later steps.

Run locally with::

    uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from legal.errors import LegalCliError

from api.errors import error_to_envelope

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

    return app


app = create_app()
