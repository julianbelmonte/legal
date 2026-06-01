"""Structured errors for legal source commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from apps.legal.models import LegalError, LegalResponse, Provenance


ErrorCode = Literal[
    "captcha_required",
    "unsupported_captcha",
    "source_unavailable",
    "network_error",
    "parse_error",
    "not_found",
    "usage_error",
    "unsupported_operation",
]

CAPTCHA_SOLVER_CAPABILITY = "captcha_solver"


@dataclass(frozen=True)
class LegalCliError(Exception):
    """Structured error suitable for JSON command output."""

    code: ErrorCode
    message: str
    retryable: bool = False
    capability_required: str | None = None
    details: dict[str, Any] | None = None
    provenance: Provenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", (self.message,))

    def to_error(self) -> LegalError:
        return LegalError(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            capability_required=self.capability_required,
            details=self.details or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return self.to_error().to_dict()

    def to_response(
        self,
        *,
        source: str,
        operation: str,
        request: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> LegalResponse:
        return LegalResponse.error_response(
            source=source,
            operation=operation,
            request=request,
            query=query,
            error=self.to_error(),
            provenance=self.provenance,
        )


def _error(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    capability_required: str | None = None,
    details: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
) -> LegalCliError:
    return LegalCliError(
        code=code,
        message=message,
        retryable=retryable,
        capability_required=capability_required,
        details=details,
        provenance=provenance,
    )


def captcha_required(message: str = "captcha solving is required for this operation") -> LegalCliError:
    return _error(
        "captcha_required",
        message,
        retryable=False,
        capability_required=CAPTCHA_SOLVER_CAPABILITY,
    )


def unsupported_captcha(message: str = "captcha solving is not supported yet") -> LegalCliError:
    return _error(
        "unsupported_captcha",
        message,
        retryable=False,
        capability_required=CAPTCHA_SOLVER_CAPABILITY,
    )


def source_unavailable(
    message: str = "source is currently unavailable",
    *,
    details: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
) -> LegalCliError:
    return _error(
        "source_unavailable",
        message,
        retryable=True,
        details=details,
        provenance=provenance,
    )


def network_error(
    message: str = "network request failed",
    *,
    details: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
) -> LegalCliError:
    return _error(
        "network_error",
        message,
        retryable=True,
        details=details,
        provenance=provenance,
    )


def parse_error(
    message: str = "source response could not be parsed",
    *,
    details: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
) -> LegalCliError:
    return _error(
        "parse_error",
        message,
        retryable=False,
        details=details,
        provenance=provenance,
    )


def not_found(
    message: str = "record was not found",
    *,
    details: dict[str, Any] | None = None,
    provenance: Provenance | None = None,
) -> LegalCliError:
    return _error(
        "not_found",
        message,
        retryable=False,
        details=details,
        provenance=provenance,
    )


def usage_error(message: str = "invalid command usage", *, details: dict[str, Any] | None = None) -> LegalCliError:
    return _error("usage_error", message, retryable=False, details=details)
