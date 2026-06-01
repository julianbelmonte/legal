"""Threadpool bridge from the async API to the sync pipeline dispatch seam.

Pipeline handlers are synchronous and may launch a browser, so they must run
off the event loop via ``run_in_threadpool``. This module runs one operation
and returns a :class:`~fastapi.responses.JSONResponse` carrying the normalized
envelope (1:1 with the CLI) and an HTTP status chosen from the error code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import legal.dispatch
from legal.errors import LegalCliError

from api.errors import envelope_and_status, error_to_envelope

__all__ = ["run"]


async def run(
    source_id: str,
    operation: str,
    params: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Run a pipeline operation off the event loop and return a JSONResponse.

    The body is always the normalized envelope. ``LegalCliError`` maps to the
    error envelope + status. Any unexpected exception is converted to a
    normalized, retryable ``source_unavailable`` envelope, mirroring the CLI's
    contract guard in :func:`legal.cli.main` so the API always returns exactly
    one JSON envelope.
    """
    try:
        result = await run_in_threadpool(
            legal.dispatch.run_operation, source_id, operation, params
        )
    except LegalCliError as error:
        env, status = error_to_envelope(error, source=source_id, operation=operation)
        return JSONResponse(content=env, status_code=status)
    except Exception as exc:  # noqa: BLE001 - contract guard, mirrors cli.main
        error = LegalCliError(
            code="source_unavailable",
            message=f"{source_id} {operation} failed unexpectedly: {type(exc).__name__}: {exc}"[:400],
            retryable=True,
            details={"exception_type": type(exc).__name__, "unexpected": True},
        )
        env, status = error_to_envelope(error, source=source_id, operation=operation)
        return JSONResponse(content=env, status_code=status)

    env, status = envelope_and_status(result)
    return JSONResponse(content=env, status_code=status)
