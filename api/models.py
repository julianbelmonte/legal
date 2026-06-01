"""Pydantic request/response models for the FastAPI consumer.

These models back the typed routers (CSJN, SAIJ) and the generic endpoint, and
document the normalized legal envelope in OpenAPI. The envelope model is
permissive (``extra="allow"``): actual response bodies are passed through
verbatim via ``JSONResponse``; the model only documents the common shape.

Keep this module import-light: no network or heavy imports at import time.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GenericOperationRequest(BaseModel):
    """JSON body for the generic ``/v1/sources/{id}/{op}`` route.

    ``params`` is the free-form parameter dict forwarded to the dispatch seam;
    ``raw`` toggles the raw passthrough. Extra params are allowed.
    """

    model_config = ConfigDict(extra="allow")

    params: dict[str, Any] = Field(default_factory=dict)
    raw: bool = False


class LegalEnvelope(BaseModel):
    """Permissive documentation model for the normalized legal envelope.

    The common top-level keys are typed optionally; ``extra="allow"`` keeps the
    model from constraining the 1:1 passthrough of actual response bodies.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool | None = None
    source: str | None = None
    operation: str | None = None
    items: Any | None = None
    document: Any | None = None
    page: Any | None = None
    facets: Any | None = None
    provenance: Any | None = None
    warnings: Any | None = None
    error: Any | None = None


class GlobalSearchRequest(BaseModel):
    """JSON body for the global cross-source search route."""

    text: str
    sources: list[str] | None = None
    all_direct: bool = False
    limit_per_source: int = 5
    raw: bool = False
