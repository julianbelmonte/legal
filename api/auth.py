"""API-key authentication dependency (fail-closed).

Every protected ``/v1`` route requires a valid API key presented in the
``x-api-key`` header. The API fails closed: when no keys are configured,
:func:`require_api_key` rejects every request with HTTP 401 so a misconfigured
deploy is never silently open. ``/healthz`` must NOT attach this dependency.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from api.settings import get_api_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """Validate the ``x-api-key`` header against the configured key set.

    Raises HTTP 401 when no keys are configured (fail-closed) or when the
    presented key is missing/invalid. Returns the matched key on success.
    """
    allowed = get_api_settings().allowed_keys()
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )
    if x_api_key is None or x_api_key not in allowed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return x_api_key


# Reusable dependency list to attach to protected routers. Do NOT attach to
# /healthz.
auth_dependencies = [Depends(require_api_key)]
