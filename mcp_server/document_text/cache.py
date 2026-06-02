"""TTL cache for extracted document text and metadata.

Paging the ``legal_get_document_text`` tool should not refetch or re-extract a
large document for every page. This module persists the **extracted text** plus
its metadata under the repo's standard cache directory (``LEGAL_CACHE_DIR`` and
the platform fallbacks in :mod:`legal.cache`), so subsequent page reads load a
cached record instead of re-dispatching a source operation.

IMPORTANT: only extracted text and metadata are stored. Raw PDF bytes are never
written here — the document text resolvers extract text before caching.

Records carry a TTL whose default comes from the step-04 MCP settings
(``mcp_server.settings.McpSettings.cache_ttl_seconds``). Cache id, source,
document reference, query hash, title/date/url metadata, full extracted text,
created/expiry timestamps, and provenance are all retained per record.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from legal.cache import (
    _coerce_utc,
    _parse_utc,
    _safe_filename,
    _utc_iso,
    get_cache_dir,
)
from legal.cache import query_hash as _query_hash
from mcp_server.settings import get_mcp_settings

JsonDict = dict[str, Any]

DOCUMENT_TEXT_DIR = "document-text"
DOCUMENT_TEXT_VERSION = 1


def _default_ttl() -> timedelta:
    """Return the configured document-text cache TTL from MCP settings."""
    return timedelta(seconds=get_mcp_settings().cache_ttl_seconds)


def new_cache_id(source: str) -> str:
    """Create a path-safe cache id with source context."""
    source_part = _safe_filename(source.lower()) or "source"
    return f"{source_part}-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class DocumentTextRecord:
    """Extracted document text plus its metadata and provenance.

    Stores only text and metadata — never raw PDF bytes.
    """

    cache_id: str
    source: str
    document_ref: JsonDict
    query_hash: str
    title: str | None
    date: str | None
    url: str | None
    text: str
    created_at: str
    expiry: str
    metadata: JsonDict = field(default_factory=dict)
    provenance: JsonDict = field(default_factory=dict)
    version: int = DOCUMENT_TEXT_VERSION

    @classmethod
    def build(
        cls,
        *,
        source: str,
        document_ref: Mapping[str, Any],
        text: str,
        metadata: Mapping[str, Any] | None = None,
        query_hash: str | None = None,
        title: str | None = None,
        date: str | None = None,
        url: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        ttl: timedelta | None = None,
        cache_id: str | None = None,
        now: datetime | None = None,
    ) -> "DocumentTextRecord":
        created = _coerce_utc(now)
        ref = dict(document_ref or {})
        return cls(
            cache_id=cache_id or new_cache_id(source),
            source=source,
            document_ref=ref,
            query_hash=query_hash if query_hash is not None else _query_hash(ref),
            title=title,
            date=date,
            url=url,
            text=text,
            created_at=_utc_iso(created),
            expiry=_utc_iso(created + (ttl if ttl is not None else _default_ttl())),
            metadata=dict(metadata or {}),
            provenance=dict(provenance or {}),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DocumentTextRecord":
        expiry = payload.get("expiry")
        if not isinstance(expiry, str):
            raise ValueError("document text record is missing expiry")
        return cls(
            version=int(payload.get("version", DOCUMENT_TEXT_VERSION)),
            cache_id=str(payload["cache_id"]),
            source=str(payload["source"]),
            document_ref=dict(payload.get("document_ref") or {}),
            query_hash=str(payload.get("query_hash", "")),
            title=payload.get("title"),
            date=payload.get("date"),
            url=payload.get("url"),
            text=str(payload.get("text", "")),
            created_at=str(payload["created_at"]),
            expiry=expiry,
            metadata=dict(payload.get("metadata") or {}),
            provenance=dict(payload.get("provenance") or {}),
        )

    def to_dict(self) -> JsonDict:
        return {
            "version": self.version,
            "cache_id": self.cache_id,
            "source": self.source,
            "document_ref": self.document_ref,
            "query_hash": self.query_hash,
            "title": self.title,
            "date": self.date,
            "url": self.url,
            "text": self.text,
            "created_at": self.created_at,
            "expiry": self.expiry,
            "metadata": self.metadata,
            "provenance": self.provenance,
        }

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return _parse_utc(self.expiry) <= _coerce_utc(now)


class DocumentTextCache:
    """Filesystem-backed TTL cache for extracted document text records."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        ttl: timedelta | None = None,
    ) -> None:
        self._base_dir = base_dir or get_cache_dir()
        self._ttl = ttl

    @property
    def directory(self) -> Path:
        return self._base_dir / DOCUMENT_TEXT_DIR

    def _path(self, cache_id: str) -> Path:
        return self.directory / f"{_safe_filename(cache_id)}.json"

    def put(
        self,
        *,
        source: str,
        document_ref: Mapping[str, Any],
        text: str,
        metadata: Mapping[str, Any] | None = None,
        query_hash: str | None = None,
        title: str | None = None,
        date: str | None = None,
        url: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        ttl: timedelta | None = None,
        cache_id: str | None = None,
        now: datetime | None = None,
    ) -> DocumentTextRecord:
        """Build, persist, and return an extracted-text cache record.

        Only ``text`` and metadata are stored; callers must extract text from
        PDF bytes before reaching this method.
        """
        record = DocumentTextRecord.build(
            source=source,
            document_ref=document_ref,
            text=text,
            metadata=metadata,
            query_hash=query_hash,
            title=title,
            date=date,
            url=url,
            provenance=provenance,
            ttl=ttl if ttl is not None else self._ttl,
            cache_id=cache_id,
            now=now,
        )
        self._write(record)
        return record

    def get(
        self,
        cache_id: str,
        *,
        now: datetime | None = None,
        delete_expired: bool = True,
    ) -> DocumentTextRecord | None:
        """Return a non-expired record by id, or ``None`` when missing/expired."""
        path = self._path(cache_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            raise ValueError("document text record must be a JSON object")
        record = DocumentTextRecord.from_dict(payload)
        if record.is_expired(now=now):
            if delete_expired:
                path.unlink(missing_ok=True)
            return None
        return record

    def delete(self, cache_id: str) -> bool:
        """Delete a cached record; return whether a file was removed."""
        path = self._path(cache_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Remove expired records and return the count deleted."""
        directory = self.directory
        if not directory.exists():
            return 0
        removed = 0
        for path in directory.glob("*.json"):
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, Mapping):
                continue
            if DocumentTextRecord.from_dict(payload).is_expired(now=now):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _write(self, record: DocumentTextRecord) -> Path:
        path = self._path(record.cache_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        return path


__all__ = [
    "DOCUMENT_TEXT_DIR",
    "DOCUMENT_TEXT_VERSION",
    "DocumentTextCache",
    "DocumentTextRecord",
    "new_cache_id",
]
