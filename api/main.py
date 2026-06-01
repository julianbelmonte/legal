"""FastAPI application for the legal data pipeline.

This module exposes the app shell: the ``create_app`` factory, an
unauthenticated ``/healthz`` endpoint, and a module-level ``app`` for uvicorn.
Routers and authentication attach in later steps.

Run locally with::

    uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI

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

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        """Unauthenticated liveness probe."""
        return {"ok": True, "service": "legal-api"}

    return app


app = create_app()
