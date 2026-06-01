"""Source adapter package for the legal CLI."""

from __future__ import annotations

from typing import NoReturn

from legal.errors import LegalCliError
from legal.models import SourceInfo, UnsupportedOperation
from legal.registry import SOURCE_BY_ID
from legal.sources.base import SourceAdapter, SourceOperation


def _protected_operation(source: SourceInfo, operation: str) -> UnsupportedOperation | None:
    for protected in source.unsupported_operations:
        if protected.operation == operation:
            return protected
    return None


def default_operation(source: SourceInfo, operation: str) -> SourceOperation:
    """Build the fallback operation used until a concrete source adapter lands."""
    if operation not in source.operations:
        raise ValueError(f"{operation!r} is not declared for source {source.id!r}")

    protected = _protected_operation(source, operation)
    if protected is not None:

        def blocked_handler(args: object) -> NoReturn:
            raise LegalCliError(
                code=protected.error_code,
                message=protected.reason or "captcha solving is not supported yet",
                retryable=False,
                capability_required=protected.capability_required,
                details={"source": source.id, "operation": operation},
            )

        return SourceOperation(
            name=operation,
            handler=blocked_handler,
            help=protected.reason,
        )

    def missing_handler(args: object) -> NoReturn:
        raise LegalCliError(
            code="unsupported_operation",
            message=f"{source.id} {operation} does not have an adapter implementation yet",
            retryable=False,
            details={"source": source.id, "operation": operation},
        )

    return SourceOperation(
        name=operation,
        handler=missing_handler,
        help=f"{operation} {source.name}",
    )


_ADAPTERS: dict[str, SourceAdapter] = {}


def get_adapter(source_id: str) -> SourceAdapter | None:
    return _ADAPTERS.get(source_id)


def register_adapter(adapter: SourceAdapter, *, replace: bool = False) -> SourceAdapter | None:
    """Register or replace a source adapter and return the previous adapter."""
    if adapter.source_id not in SOURCE_BY_ID:
        raise ValueError(f"unknown source adapter {adapter.source_id!r}")
    if not replace and adapter.source_id in _ADAPTERS:
        raise ValueError(f"source adapter {adapter.source_id!r} is already registered")
    previous = _ADAPTERS.get(adapter.source_id)
    _ADAPTERS[adapter.source_id] = adapter
    return previous


def unregister_adapter(source_id: str) -> SourceAdapter | None:
    return _ADAPTERS.pop(source_id, None)


__all__ = [
    "SourceAdapter",
    "SourceOperation",
    "default_operation",
    "get_adapter",
    "register_adapter",
    "unregister_adapter",
]


def _register_builtin_adapters() -> None:
    from legal.sources import aaip as _aaip  # noqa: F401
    from legal.sources import bcra as _bcra  # noqa: F401
    from legal.sources import bo_nacional as _bo_nacional  # noqa: F401
    from legal.sources import bo_pba as _bo_pba  # noqa: F401
    from legal.sources import cnacaf as _cnacaf  # noqa: F401
    from legal.sources import csjn as _csjn  # noqa: F401
    from legal.sources import dppj as _dppj  # noqa: F401
    from legal.sources import igj as _igj  # noqa: F401
    from legal.sources import infoleg as _infoleg  # noqa: F401
    from legal.sources import juba as _juba  # noqa: F401
    from legal.sources import jusbaires as _jusbaires  # noqa: F401
    from legal.sources import normas_pba as _normas_pba  # noqa: F401
    from legal.sources import pjn_expedientes as _pjn_exp  # noqa: F401
    from legal.sources import pjn_juris as _pjn_juris  # noqa: F401
    from legal.sources import ptn as _ptn  # noqa: F401
    from legal.sources import saij as _saij  # noqa: F401
    from legal.sources import sentencias_scba as _sentencias_scba  # noqa: F401
    from legal.sources import tfn as _tfn  # noqa: F401


_register_builtin_adapters()
