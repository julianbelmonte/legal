"""Generic, source-agnostic router: ``POST /v1/sources/{source_id}/{operation}``.

This is the uniform core of the API. It exposes every registry source/operation
pair through a single authenticated endpoint and returns the pipeline's
normalized envelope, giving full 1:1 parity with the CLI.

The path is validated against the registry before dispatch; an unknown source or
operation is funneled through the threadpool runner, which lets
``run_operation``/``resolve_operation`` raise a ``LegalCliError`` that maps to a
normalized usage/not-found envelope (never an unhandled 500).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import api.runner
from api.auth import auth_dependencies
from api.models import GenericOperationRequest

router = APIRouter(prefix="/v1", tags=["sources"], dependencies=auth_dependencies)


@router.post("/sources/{source_id}/{operation}")
async def run_generic_operation(
    source_id: str,
    operation: str,
    body: GenericOperationRequest,
) -> JSONResponse:
    """Run any registry source/operation from a free-form params dict.

    The request ``params`` are forwarded to the dispatch seam with ``raw`` merged
    in. Validation of ``source_id``/``operation`` happens inside the dispatch
    seam (``resolve_operation``), whose ``LegalCliError`` the runner maps to the
    normalized usage/not-found envelope.
    """
    params = {**body.params, "raw": body.raw}
    return await api.runner.run(source_id, operation, params)
