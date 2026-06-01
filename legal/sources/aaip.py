"""AAIP public Google Sheet adapter."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.legal.cache import ensure_cache_dir, get_cache_dir
from apps.legal.errors import LegalCliError, not_found, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import LegalDocument, LegalItem, LegalResponse, Provenance
from apps.legal.pagination import build_page_info, decode_cursor
from apps.legal.parsing import absolute_url, classify_link, clean_text, normalize_date
from apps.legal.registry import get_source
from apps.legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "aaip"
SOURCE_MAP = "apps/legal/docs/aaip_disposiciones.md"
HUMAN_URL = "https://www.argentina.gob.ar/aaip/buscador-normativa"
SHEET_URL = (
    "https://sheets.googleapis.com/v4/spreadsheets/"
    "1ssr92BY3h4nBTEaCsdaXTsZByr0W01uHvXvgpr-Yzyk/values/Hoja%202"
    "?alt=json&key=AIzaSyCq2wEEKL9-6RmX-TkW23qJsrmnFHFf5tY"
)

CACHE_VERSION = 1
CACHE_FILENAME = "sheet.json"
CACHE_TTL = timedelta(hours=24)
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_SYNC_LIMIT = 20
SNIPPET_LENGTH = 240

_COLUMN_FIELDS = {
    "filtro-tipo": "type",
    "numero": "number",
    "descripcion": "description",
    "filtro-categoria": "category",
    "estado": "status",
    "btn-ver": "text_url",
    "btn-ver-mas": "modification_derogation_url",
    "fecha-pub-der": "publication_date_raw",
}
_SEARCH_FIELDS = ("description", "type", "number", "category", "status")
_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_ID_RE = re.compile(r"[^a-z0-9_.:-]+")

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AaipSheet:
    """Fetched or cached AAIP sheet data."""

    records: list[JsonDict]
    values: list[list[str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    fetched_at: str = ""
    fetched_url: str = SHEET_URL
    sheet_range: str | None = None
    major_dimension: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    cache_path: Path | None = None
    from_cache: bool = False


def add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cached",
        action="store_true",
        help="use a fresh cached sheet when available instead of fetching",
    )


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", help="free-text filter over description, type, number, category, and status")
    parser.add_argument("--tipo", help="filter by AAIP type")
    parser.add_argument("--numero", help="filter by AAIP number")
    parser.add_argument("--categoria", help="filter by AAIP category")
    parser.add_argument("--estado", help="filter by AAIP status")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("record_id", nargs="?", help="stable AAIP item id returned by search")
    parser.add_argument("--numero", help="find the first AAIP item with this number")
    parser.add_argument("--tipo", help="disambiguate --numero by AAIP type")
    parser.add_argument(
        "--fetch-infoleg",
        action="store_true",
        help="reserved for future Infoleg handoff; current output includes Infoleg links",
    )


def handle_sync(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="sync")
    sheet = load_sheet(force=not args.cached)
    limit = args.limit or int(cursor_payload.get("limit") or DEFAULT_SYNC_LIMIT)
    offset = int(cursor_payload.get("offset") or 0)
    records = sheet.records[offset : offset + limit]
    items = [_record_to_item(record, sheet=sheet, include_raw=args.raw) for record in records]
    page = build_page_info(
        source=SOURCE_ID,
        operation="sync",
        limit=limit,
        offset=offset,
        total=len(sheet.records),
        item_count=len(items),
    )
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="sync",
        query={"cached": bool(args.cached), "limit": limit, "offset": offset},
        items=items,
        page=page,
        provenance=_sheet_provenance(sheet),
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = args.limit or int(cursor_payload.get("limit") or DEFAULT_SEARCH_LIMIT)
    offset = int(cursor_payload.get("offset") or 0)

    sheet = load_sheet()
    matches = filter_records(
        sheet.records,
        text=query.get("text"),
        tipo=query.get("tipo"),
        numero=query.get("numero"),
        categoria=query.get("categoria"),
        estado=query.get("estado"),
    )
    page_records = matches[offset : offset + limit]
    items = [_record_to_item(record, sheet=sheet, include_raw=args.raw) for record in page_records]
    has_more = offset + len(page_records) < len(matches)
    page = build_page_info(
        source=SOURCE_ID,
        operation="search",
        limit=limit,
        offset=offset,
        total=len(matches),
        item_count=len(items),
        has_more=has_more,
        raw={"query": query} if has_more else None,
    )
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "limit": limit, "offset": offset},
        items=items,
        page=page,
        provenance=_sheet_provenance(sheet),
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    if not args.record_id and not args.numero:
        raise usage_error("either record_id or --numero is required")

    sheet = load_sheet()
    record, warnings = find_record(
        sheet.records,
        record_id=args.record_id,
        numero=args.numero,
        tipo=args.tipo,
    )
    if record is None:
        raise not_found(
            "AAIP record was not found",
            details={"id": args.record_id, "numero": args.numero, "tipo": args.tipo},
            provenance=_sheet_provenance(sheet),
        )
    if args.fetch_infoleg:
        warnings.append("fetch-infoleg is not implemented yet; returning AAIP metadata with Infoleg links")

    document = _record_to_document(record, sheet=sheet, include_raw=args.raw)
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={
            "id": args.record_id,
            "numero": args.numero,
            "tipo": args.tipo,
            "fetch_infoleg": bool(args.fetch_infoleg),
        },
        document=document,
        provenance=document.provenance,
        warnings=warnings,
    )


def load_sheet(
    *,
    force: bool = False,
    client: LegalHttpClient | None = None,
    base_dir: Path | None = None,
) -> AaipSheet:
    """Load the AAIP sheet from cache or fetch and cache it."""
    if not force:
        cached = read_sheet_cache(base_dir=base_dir)
        if cached is not None and not _is_stale(cached.fetched_at):
            return cached
    return fetch_sheet(client=client, base_dir=base_dir)


def fetch_sheet(
    *,
    client: LegalHttpClient | None = None,
    base_dir: Path | None = None,
) -> AaipSheet:
    """Fetch the public AAIP Google Sheet and persist a normalized cache."""
    owns_client = client is None
    http = client or LegalHttpClient()
    try:
        response = http.request("GET", SHEET_URL)
        try:
            payload = response.json()
        except ValueError as exc:
            raise parse_error(
                "AAIP sheet response was not valid JSON",
                details={"url": str(response.url), "status_code": response.status_code},
                provenance=Provenance.now(
                    source_urls=[HUMAN_URL, SHEET_URL],
                    fetched_urls=[str(response.url)],
                    source_map=SOURCE_MAP,
                    raw={"status_code": response.status_code},
                ),
            ) from exc
    finally:
        if owns_client:
            http.close()

    sheet = parse_sheet_payload(
        payload,
        fetched_at=_utc_iso(),
        fetched_url=str(response.url),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )
    cache_path = write_sheet_cache(sheet, base_dir=base_dir)
    return _replace_cache_metadata(sheet, cache_path=cache_path, from_cache=False)


def parse_sheet_payload(
    payload: Any,
    *,
    fetched_at: str | None = None,
    fetched_url: str = SHEET_URL,
    etag: str | None = None,
    last_modified: str | None = None,
) -> AaipSheet:
    """Normalize the Google Sheets values payload into AAIP records."""
    if not isinstance(payload, Mapping):
        raise parse_error("AAIP sheet payload must be a JSON object")

    values = _coerce_values(payload.get("values"))
    if len(values) < 2:
        raise parse_error(
            "AAIP sheet payload does not include header rows",
            details={"range": payload.get("range"), "row_count": len(values)},
        )

    headers = [_canonical_header(value) for value in values[0]]
    missing = sorted(header for header in _COLUMN_FIELDS if header not in headers)
    if missing:
        raise parse_error(
            "AAIP sheet payload is missing expected columns",
            details={"missing": missing, "headers": values[0], "canonical_headers": headers},
        )

    records: list[JsonDict] = []
    for index, row in enumerate(values[2:], start=3):
        record = _record_from_row(row, headers=headers, row_number=index)
        if record is not None:
            records.append(record)

    return AaipSheet(
        records=records,
        values=values,
        headers=headers,
        fetched_at=fetched_at or _utc_iso(),
        fetched_url=fetched_url,
        sheet_range=_optional_text(payload.get("range")),
        major_dimension=_optional_text(payload.get("majorDimension")),
        etag=etag,
        last_modified=last_modified,
    )


def read_sheet_cache(*, base_dir: Path | None = None) -> AaipSheet | None:
    path = sheet_cache_path(base_dir=base_dir)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise parse_error("AAIP sheet cache could not be read", details={"path": str(path), "error": str(exc)}) from exc
    except json.JSONDecodeError as exc:
        raise parse_error("AAIP sheet cache is not valid JSON", details={"path": str(path)}) from exc

    if not isinstance(payload, Mapping):
        raise parse_error("AAIP sheet cache must be a JSON object", details={"path": str(path)})
    if payload.get("version") != CACHE_VERSION or payload.get("source") != SOURCE_ID:
        raise parse_error(
            "AAIP sheet cache has an unsupported format",
            details={"path": str(path), "version": payload.get("version"), "source": payload.get("source")},
        )

    records = payload.get("records")
    values = payload.get("values")
    if not isinstance(records, list):
        raise parse_error("AAIP sheet cache is missing records", details={"path": str(path)})
    if any(not isinstance(record, Mapping) for record in records):
        raise parse_error("AAIP sheet cache records must be JSON objects", details={"path": str(path)})

    return AaipSheet(
        records=[dict(record) for record in records if isinstance(record, Mapping)],
        values=_coerce_values(values or []),
        headers=[str(header) for header in payload.get("headers") or []],
        fetched_at=str(payload.get("fetched_at") or ""),
        fetched_url=str(payload.get("fetched_url") or SHEET_URL),
        sheet_range=_optional_text(payload.get("sheet_range")),
        major_dimension=_optional_text(payload.get("major_dimension")),
        etag=_optional_text(payload.get("etag")),
        last_modified=_optional_text(payload.get("last_modified")),
        cache_path=path,
        from_cache=True,
    )


def write_sheet_cache(sheet: AaipSheet, *, base_dir: Path | None = None) -> Path:
    path = sheet_cache_path(base_dir=base_dir, create=True)
    payload: JsonDict = {
        "version": CACHE_VERSION,
        "source": SOURCE_ID,
        "fetched_at": sheet.fetched_at,
        "fetched_url": sheet.fetched_url,
        "source_url": SHEET_URL,
        "sheet_range": sheet.sheet_range,
        "major_dimension": sheet.major_dimension,
        "etag": sheet.etag,
        "last_modified": sheet.last_modified,
        "headers": sheet.headers,
        "values": sheet.values,
        "records": sheet.records,
    }
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def sheet_cache_path(*, base_dir: Path | None = None, create: bool = False) -> Path:
    base = Path(base_dir) if base_dir is not None else (ensure_cache_dir() if create else get_cache_dir())
    path = base / "sources" / SOURCE_ID / CACHE_FILENAME
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def filter_records(
    records: Sequence[Mapping[str, Any]],
    *,
    text: str | None = None,
    tipo: str | None = None,
    numero: str | None = None,
    categoria: str | None = None,
    estado: str | None = None,
) -> list[JsonDict]:
    filters = {
        "type": tipo,
        "number": numero,
        "category": categoria,
        "status": estado,
    }
    return [
        dict(record)
        for record in records
        if _matches_filters(record, filters=filters) and _matches_text(record, text)
    ]


def find_record(
    records: Sequence[Mapping[str, Any]],
    *,
    record_id: str | None = None,
    numero: str | None = None,
    tipo: str | None = None,
) -> tuple[JsonDict | None, list[str]]:
    warnings: list[str] = []

    if record_id:
        for record in records:
            if _text(record.get("id")) == record_id:
                return dict(record), warnings
        if numero is None and not record_id.startswith(f"{SOURCE_ID}:"):
            numero = record_id

    if numero:
        matches = filter_records(records, numero=numero, tipo=tipo)
        if matches:
            if len(matches) > 1:
                warnings.append("multiple AAIP records matched; returning the first result")
            return matches[0], warnings

    return None, warnings


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("AAIP source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("sync", handle_sync, help="fetch and cache the AAIP public sheet", add_arguments=add_sync_arguments)
    adapter.register_operation("search", handle_search, help="search the cached AAIP sheet", add_arguments=add_search_arguments)
    adapter.register_operation("get", handle_get, help="get AAIP normalized metadata", add_arguments=add_get_arguments)
    return adapter


def _coerce_values(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        raise parse_error("AAIP sheet values must be a list")
    rows: list[list[str]] = []
    for row in value:
        if not isinstance(row, list):
            raise parse_error("AAIP sheet row must be a list")
        rows.append([str(cell) for cell in row])
    return rows


def _record_from_row(row: list[str], *, headers: list[str], row_number: int) -> JsonDict | None:
    source_fields: JsonDict = {}
    for header, field_name in _COLUMN_FIELDS.items():
        index = headers.index(header)
        source_fields[field_name] = clean_text(row[index]) if index < len(row) else None

    if not any(source_fields.values()):
        return None

    text_url = absolute_url(HUMAN_URL, source_fields.get("text_url"))
    modification_url = absolute_url(HUMAN_URL, source_fields.get("modification_derogation_url"))
    publication_date_raw = source_fields.get("publication_date_raw")
    publication_date = normalize_date(publication_date_raw)
    record: JsonDict = {
        "id": _record_id(source_fields, row_number=row_number),
        "row_number": row_number,
        "type": source_fields.get("type"),
        "number": source_fields.get("number"),
        "description": source_fields.get("description"),
        "category": source_fields.get("category"),
        "status": source_fields.get("status"),
        "text_url": text_url,
        "modification_derogation_url": modification_url,
        "publication_date": publication_date,
        "publication_date_raw": publication_date_raw,
    }
    record["links"] = _record_links(record)
    record["source_fields"] = {
        "type": record["type"],
        "number": record["number"],
        "description": record["description"],
        "category": record["category"],
        "status": record["status"],
        "texto_url": text_url,
        "modificacion_derogacion_url": modification_url,
        "fecha_publicacion_derogacion": publication_date_raw,
        "publication_date": publication_date,
    }
    record["raw"] = {
        "row_number": row_number,
        "cells": row,
        "fields": source_fields,
    }
    return record


def _record_id(fields: Mapping[str, Any], *, row_number: int) -> str:
    parts = [_slug(fields.get("type")), _slug(fields.get("number"))]
    stem = "-".join(part for part in parts if part)
    if stem:
        return f"{SOURCE_ID}:{stem}:r{row_number}"
    return f"{SOURCE_ID}:r{row_number}"


def _record_to_item(record: Mapping[str, Any], *, sheet: AaipSheet, include_raw: bool = False) -> LegalItem:
    source_fields = dict(record.get("source_fields") or {})
    return LegalItem(
        id=_text(record.get("id")),
        title=_record_title(record),
        date=_optional_text(record.get("publication_date")),
        document_type=_optional_text(record.get("type")),
        url=_primary_url(record),
        snippet=_snippet(record.get("description")),
        facets={
            "type": _optional_text(record.get("type")),
            "category": _optional_text(record.get("category")),
            "status": _optional_text(record.get("status")),
        },
        source_fields={
            **source_fields,
            "links": list(record.get("links") or []),
        },
        raw=dict(record.get("raw") or {}) if include_raw else {},
        provenance=_record_provenance(sheet, record),
    )


def _record_to_document(record: Mapping[str, Any], *, sheet: AaipSheet, include_raw: bool = False) -> LegalDocument:
    links = list(record.get("links") or [])
    metadata = {
        "type": _optional_text(record.get("type")),
        "number": _optional_text(record.get("number")),
        "description": _optional_text(record.get("description")),
        "category": _optional_text(record.get("category")),
        "status": _optional_text(record.get("status")),
        "publication_date": _optional_text(record.get("publication_date")),
        "publication_date_raw": _optional_text(record.get("publication_date_raw")),
        "text_url": _optional_text(record.get("text_url")),
        "modification_derogation_url": _optional_text(record.get("modification_derogation_url")),
        "text_url_is_infoleg": _is_infoleg_url(record.get("text_url")),
    }
    return LegalDocument(
        id=_text(record.get("id")),
        title=_record_title(record),
        date=_optional_text(record.get("publication_date")),
        document_type=_optional_text(record.get("type")),
        body=_optional_text(record.get("description")),
        url=_primary_url(record),
        metadata=metadata,
        links=links,
        source_fields=dict(record.get("source_fields") or {}),
        raw=dict(record.get("raw") or {}) if include_raw else {},
        provenance=_record_provenance(sheet, record),
    )


def _record_links(record: Mapping[str, Any]) -> list[JsonDict]:
    links: list[JsonDict] = []
    for field_name, label in (
        ("text_url", "texto"),
        ("modification_derogation_url", "modificacion_derogacion"),
    ):
        url = _optional_text(record.get(field_name))
        if not url:
            continue
        links.append(
            {
                "label": label,
                "url": url,
                "kind": classify_link(url, base_url=HUMAN_URL),
                "field": field_name,
                "infoleg": _is_infoleg_url(url),
            }
        )
    return links


def _record_title(record: Mapping[str, Any]) -> str | None:
    type_value = _optional_text(record.get("type"))
    number = _optional_text(record.get("number"))
    if type_value and number:
        return f"{type_value} {number}"
    return type_value or number or _optional_text(record.get("description"))


def _primary_url(record: Mapping[str, Any]) -> str | None:
    return _optional_text(record.get("text_url")) or _optional_text(record.get("modification_derogation_url"))


def _sheet_provenance(sheet: AaipSheet) -> Provenance:
    return Provenance(
        source_urls=[HUMAN_URL, SHEET_URL],
        fetched_urls=[sheet.fetched_url],
        fetched_at=sheet.fetched_at or _utc_iso(),
        source_map=SOURCE_MAP,
        source_response_id=sheet.sheet_range,
        raw={
            "range": sheet.sheet_range,
            "record_count": len(sheet.records),
            "cache_path": str(sheet.cache_path) if sheet.cache_path else None,
            "from_cache": sheet.from_cache,
            "etag": sheet.etag,
            "last_modified": sheet.last_modified,
        },
    )


def _record_provenance(sheet: AaipSheet, record: Mapping[str, Any]) -> Provenance:
    return Provenance(
        source_urls=[HUMAN_URL, SHEET_URL],
        fetched_urls=[sheet.fetched_url],
        fetched_at=sheet.fetched_at or _utc_iso(),
        source_map=SOURCE_MAP,
        source_response_id=_text(record.get("id")),
        raw={
            "range": sheet.sheet_range,
            "row_number": record.get("row_number"),
            "from_cache": sheet.from_cache,
        },
    )


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw_query = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw_query, Mapping) and isinstance(raw_query.get("query"), Mapping):
        return {str(key): value for key, value in raw_query["query"].items() if value not in (None, "")}
    query = {
        "text": args.text,
        "tipo": args.tipo,
        "numero": args.numero,
        "categoria": args.categoria,
        "estado": args.estado,
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _matches_filters(record: Mapping[str, Any], *, filters: Mapping[str, str | None]) -> bool:
    for field_name, expected in filters.items():
        if expected in (None, ""):
            continue
        actual = record.get(field_name)
        if field_name == "number":
            if _compact(actual) != _compact(expected):
                return False
            continue
        if _search_key(actual) != _search_key(expected):
            return False
    return True


def _matches_text(record: Mapping[str, Any], text: str | None) -> bool:
    terms = _search_key(text).split()
    if not terms:
        return True
    haystack = " ".join(_search_key(record.get(field_name)) for field_name in _SEARCH_FIELDS)
    return all(term in haystack for term in terms)


def _canonical_header(value: Any) -> str:
    return _search_key(value).replace(" ", "-")


def _search_key(value: Any) -> str:
    text = clean_text(str(value)) if value is not None else None
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _compact(value: Any) -> str:
    return _search_key(value).replace(" ", "")


def _slug(value: Any) -> str:
    slug = _ID_RE.sub("-", _search_key(value).replace(" ", "-")).strip("-")
    return slug[:80]


def _snippet(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or len(text) <= SNIPPET_LENGTH:
        return text
    prefix = text[:SNIPPET_LENGTH].rstrip()
    boundary = prefix.rfind(" ")
    if boundary >= SNIPPET_LENGTH // 2:
        prefix = prefix[:boundary].rstrip()
    return f"{prefix}..."


def _text(value: Any) -> str:
    return clean_text(str(value)) or ""


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _is_infoleg_url(value: Any) -> bool:
    url = _optional_text(value)
    if not url:
        return False
    return "infoleg.gob.ar" in urlparse(url).netloc.lower()


def _is_stale(fetched_at: str) -> bool:
    parsed = _parse_utc(fetched_at)
    if parsed is None:
        return True
    return parsed + CACHE_TTL <= datetime.now(timezone.utc)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _replace_cache_metadata(sheet: AaipSheet, *, cache_path: Path | None, from_cache: bool) -> AaipSheet:
    return AaipSheet(
        records=sheet.records,
        values=sheet.values,
        headers=sheet.headers,
        fetched_at=sheet.fetched_at,
        fetched_url=sheet.fetched_url,
        sheet_range=sheet.sheet_range,
        major_dimension=sheet.major_dimension,
        etag=sheet.etag,
        last_modified=sheet.last_modified,
        cache_path=cache_path,
        from_cache=from_cache,
    )


register_adapter(build_adapter(), replace=True)
