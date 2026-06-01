"""Typed JSON models shared by legal source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


JsonDict = dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    """Return a JSON-compatible value while preserving upstream raw fields."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
            if getattr(value, item.name) is not None
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def model_dict(instance: Any) -> JsonDict:
    """Serialize a legal model to a JSON-compatible dict.

    Optional dataclass fields set to ``None`` are omitted. Values inside raw
    upstream dictionaries are preserved, including explicit JSON nulls.
    """
    if not is_dataclass(instance) or isinstance(instance, type):
        raise TypeError(f"expected dataclass instance, got {type(instance).__name__}")
    return _jsonable(instance)


@dataclass(frozen=True)
class UnsupportedOperation:
    operation: str
    error_code: str
    capability_required: str | None = None
    reason: str | None = None

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    operations: list[str] = field(default_factory=list)
    source_map: str | None = None
    notes: str | None = None
    capabilities: list[str] = field(default_factory=list)
    browser_required: bool = False
    unsupported_operations: list[UnsupportedOperation] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class PageInfo:
    limit: int | None = None
    offset: int | None = None
    page: int | None = None
    total: int | None = None
    has_more: bool = False
    next_cursor: str | None = None
    search_id: str | None = None

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class Provenance:
    source_urls: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=_utc_now)
    source_map: str | None = None
    source_response_id: str | None = None
    raw: JsonDict = field(default_factory=dict)

    @classmethod
    def now(cls, **kwargs: Any) -> "Provenance":
        return cls(fetched_at=_utc_now(), **kwargs)

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class LegalItem:
    id: str
    title: str | None = None
    date: str | None = None
    document_type: str | None = None
    url: str | None = None
    file_url: str | None = None
    snippet: str | None = None
    facets: JsonDict = field(default_factory=dict)
    source_fields: JsonDict = field(default_factory=dict)
    raw: JsonDict = field(default_factory=dict)
    provenance: Provenance | None = None

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class LegalDocument:
    id: str
    title: str | None = None
    date: str | None = None
    document_type: str | None = None
    body: str | None = None
    url: str | None = None
    file_url: str | None = None
    content_type: str | None = None
    text_format: str | None = None
    metadata: JsonDict = field(default_factory=dict)
    links: list[JsonDict] = field(default_factory=list)
    files: list[JsonDict] = field(default_factory=list)
    source_fields: JsonDict = field(default_factory=dict)
    raw: JsonDict = field(default_factory=dict)
    provenance: Provenance | None = None

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class LegalError:
    code: str
    message: str
    retryable: bool = False
    capability_required: str | None = None
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class LegalResponse:
    ok: bool
    source: str
    operation: str
    query: JsonDict | None = None
    request: JsonDict | None = None
    items: list[LegalItem] | None = None
    document: LegalDocument | None = None
    facets: JsonDict | None = None
    page: PageInfo | None = None
    provenance: Provenance | None = None
    warnings: list[str] = field(default_factory=list)
    error: LegalError | None = None

    @classmethod
    def search(
        cls,
        *,
        source: str,
        operation: str = "search",
        query: JsonDict | None = None,
        items: list[LegalItem] | None = None,
        page: PageInfo | None = None,
        provenance: Provenance | None = None,
        warnings: list[str] | None = None,
        facets: JsonDict | None = None,
    ) -> "LegalResponse":
        return cls(
            ok=True,
            source=source,
            operation=operation,
            query=query or {},
            items=items or [],
            page=page,
            provenance=provenance or Provenance(),
            warnings=warnings or [],
            facets=facets,
        )

    @classmethod
    def document_response(
        cls,
        *,
        source: str,
        operation: str = "get",
        request: JsonDict | None = None,
        document: LegalDocument,
        provenance: Provenance | None = None,
        warnings: list[str] | None = None,
    ) -> "LegalResponse":
        return cls(
            ok=True,
            source=source,
            operation=operation,
            request=request or {},
            document=document,
            provenance=provenance or document.provenance or Provenance(),
            warnings=warnings or [],
        )

    @classmethod
    def error_response(
        cls,
        *,
        source: str,
        operation: str,
        error: LegalError,
        request: JsonDict | None = None,
        query: JsonDict | None = None,
        provenance: Provenance | None = None,
        warnings: list[str] | None = None,
    ) -> "LegalResponse":
        return cls(
            ok=False,
            source=source,
            operation=operation,
            request=request,
            query=query,
            provenance=provenance,
            warnings=warnings or [],
            error=error,
        )

    def to_dict(self) -> JsonDict:
        return model_dict(self)


@dataclass(frozen=True)
class CursorState:
    source: str
    operation: str
    query: JsonDict
    state: JsonDict
    page: PageInfo | None = None
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> JsonDict:
        return model_dict(self)
