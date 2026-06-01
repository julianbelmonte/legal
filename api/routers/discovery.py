"""Discovery router: ``/v1/sources``, ``/v1/sources/{source_id}``, ``/v1/schema``.

These read-only endpoints mirror the CLI's ``sources`` and ``schema`` commands so
agentic clients can discover the wired source registry and the response envelope
schema before invoking operations. The data is pure (no network), but the routes
stay behind the same API-key auth as the rest of the ``/v1`` surface for a
consistent contract.

Note on path coexistence: ``GET /v1/sources/{source_id}`` (this router) and
``POST /v1/sources/{source_id}/{operation}`` (the generic router) share the
``/v1/sources/...`` prefix but never collide -- they differ by method and by path
arity, so FastAPI routes each independently.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

import legal.registry
import legal.schema
from legal.errors import not_found

from api.auth import auth_dependencies

router = APIRouter(prefix="/v1", tags=["discovery"], dependencies=auth_dependencies)


@router.get("/sources")
def list_sources() -> dict[str, Any]:
    """Return the wired source registry as ``{"items": [...]}``.

    Each item is a source dict (operations advertised, unsupported operations
    annotated), exactly as the CLI's ``sources`` command emits.
    """
    return {"items": legal.registry.list_sources()}


@router.get("/sources/{source_id}")
def get_source(source_id: str) -> dict[str, Any]:
    """Return a single source dict, or a normalized not-found envelope.

    Unknown ``source_id`` yields the same ``ok: false`` error envelope the CLI
    produces, rather than an HTTP error, keeping the body 1:1 with the pipeline.
    """
    source = legal.registry.get_source(source_id)
    if source is None:
        return not_found(
            f"unknown source: {source_id}",
            details={"source_id": source_id},
        ).to_response(source=source_id, operation="get_source").to_dict()
    return source.to_dict()


@router.get("/schema")
def get_schema() -> dict[str, Any]:
    """Return the normalized response envelope schema."""
    return legal.schema.LEGAL_RESPONSE_SCHEMA
