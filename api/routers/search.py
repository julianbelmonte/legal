"""Global cross-source search router: ``POST /v1/search``.

This is a thin typed wrapper over the shared global-search core
(``legal.global_search.run_global_search``), which fans a query out across the
selected direct sources and aggregates the results into the same normalized
envelope the CLI's ``search`` command returns (1:1 parity).

The fan-out is sync and HTTP-backed, so it runs in the threadpool. Source
selection (``all_direct`` or a non-empty ``sources`` list) is validated by the
shared selector inside ``run_global_search``, which raises a ``LegalCliError``
(``usage_error``) that the app's exception handler maps to a normalized error
envelope.
"""

from __future__ import annotations

import legal.global_search
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.auth import auth_dependencies
from api.errors import envelope_and_status
from api.models import GlobalSearchRequest

router = APIRouter(prefix="/v1", tags=["search"], dependencies=auth_dependencies)


@router.post("/search")
async def global_search(body: GlobalSearchRequest) -> JSONResponse:
    """Run the global fan-out search and return the aggregated envelope.

    Requires a source selector: either ``all_direct=True`` or a non-empty
    ``sources`` list. When neither is provided, ``run_global_search`` raises a
    ``LegalCliError`` (``usage_error``) which maps to a normalized error
    envelope. The core is referenced as ``legal.global_search.run_global_search``
    (module attribute) so test patching works.
    """
    from fastapi.concurrency import run_in_threadpool

    result = await run_in_threadpool(
        legal.global_search.run_global_search,
        text=body.text,
        sources=body.sources,
        all_direct=body.all_direct,
        limit_per_source=body.limit_per_source,
        raw=body.raw,
    )
    env, status = envelope_and_status(result)
    return JSONResponse(content=env, status_code=status)
