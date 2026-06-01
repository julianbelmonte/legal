"""TFN jurisprudence API adapter."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from email.message import Message
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from legal.errors import LegalCliError, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import decode_cursor, make_cursor, page_info_from_offset
from legal.parsing import classify_link, clean_snippet, clean_text, normalize_date
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "tfn"
SOURCE_MAP = "legal/docs/tfn_jurisprudencia.md"

HUMAN_URL = "https://jurisprudenciatfn.mecon.gob.ar/"
API_BASE_URL = "https://api.jurisprudencia-tfn.ar"
FILTERS_URL = f"{API_BASE_URL}/filters"
SEARCH_STATS_URL = f"{API_BASE_URL}/searchStats"
LATEST_CASES_URL = f"{API_BASE_URL}/latestCases"
HYBRID_SEARCH_URL = f"{API_BASE_URL}/hybridSearch"
SUMMARY_URL_TEMPLATE = f"{API_BASE_URL}/cases/{{fallo_id}}/ai-summary"
PDF_URL_TEMPLATE = f"{API_BASE_URL}/pdf/{{fallo_id}}"

DEFAULT_LIMIT = 10
DEFAULT_SEARCH_IN = "objetos"
SNIPPET_LENGTH = 420
SEARCH_IN_VALUES = {"objetos", "doctrinas"}
TRIBUNAL_VALUES = {"cncaf", "tfn"}
COMPETENCIA_VALUES = {"aduanera", "impositiva"}
SUMMARY_FIELDS = (
    "hechos",
    "argumentos_partes",
    "decision_tribunal",
    "fundamentos_principales",
    "disidencias",
)
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/(?P<total>\d+)\s*$")


@dataclass(frozen=True)
class TfnJsonResponse:
    payload: Any
    fetched_url: str
    headers: JsonDict


@dataclass(frozen=True)
class TfnPdfResponse:
    response: httpx.Response
    fallback_from: JsonDict | None = None


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", "--text", "--q", dest="query", help="free text query")
    parser.add_argument(
        "--search-in",
        default=DEFAULT_SEARCH_IN,
        help="TFN hybrid search field: objetos or doctrinas",
    )
    parser.add_argument("--tribunal", action="append", help="tribunal filter; repeatable: tfn or cncaf")
    parser.add_argument("--registro", help="registro filter")
    parser.add_argument("--expediente", help="expediente filter")
    parser.add_argument("--caratula", help="caratula filter")
    parser.add_argument("--sala", action="append", help="sala filter; repeatable")
    parser.add_argument("--vocalia", action="append", help="vocalia filter; repeatable, 1 through 21")
    parser.add_argument("--competencia", action="append", help="competencia filter; repeatable")
    parser.add_argument("--from", dest="date_from", help="decision date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="decision date upper bound, YYYY-MM-DD")
    parser.add_argument("--honorarios", action="store_true", help="filter cases with fee regulation")


def add_latest_arguments(parser: argparse.ArgumentParser) -> None:
    parser.description = "List latest TFN/CNCAF cases from the public API."


def add_summary_arguments(parser: argparse.ArgumentParser) -> None:
    _add_fallo_id_arguments(parser, "summary")


def add_pdf_arguments(parser: argparse.ArgumentParser) -> None:
    _add_fallo_id_arguments(parser, "pdf")


def handle_filters(args: argparse.Namespace) -> LegalResponse:
    with _make_client() as client:
        filters = fetch_json(FILTERS_URL, client=client)
        stats = fetch_json(SEARCH_STATS_URL, client=client)

    facets = parse_filters_payload(filters.payload, stats=stats.payload)
    raw: JsonDict = {
        "headers": {
            "filters": filters.headers,
            "searchStats": stats.headers,
        },
        "stats": stats.payload if isinstance(stats.payload, Mapping) else None,
    }
    if bool(args.raw):
        raw["filters_payload"] = filters.payload
        raw["search_stats_payload"] = stats.payload
    return LegalResponse(
        ok=True,
        source=SOURCE_ID,
        operation="filters",
        query={},
        facets=facets,
        provenance=_provenance(
            fetched_urls=[filters.fetched_url, stats.fetched_url],
            source_response_id="filters",
            raw=_compact(raw),
        ),
        warnings=[],
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None), operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    offset = _cursor_offset(cursor_payload)
    limit = int(getattr(args, "limit", None) or cursor_payload.get("limit") or DEFAULT_LIMIT)
    request_limit = offset + limit + 1
    body = search_body(query, limit=request_limit)

    with _make_client() as client:
        search_response = post_search(body, client=client)

    hits = _case_list(search_response.payload, field="results", fetched_url=search_response.fetched_url)
    selected_hits = hits[offset : offset + limit]
    items = [
        case_to_item(hit, fetched_url=search_response.fetched_url, include_raw=bool(args.raw))
        for hit in selected_hits
    ]
    has_more = len(hits) > offset + len(items)
    next_cursor = (
        make_cursor(
            source=SOURCE_ID,
            operation="search",
            offset=offset + len(items),
            limit=limit,
            raw={"query": query},
        )
        if has_more and items
        else None
    )

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query=_response_query(query, limit=limit, offset=offset),
        items=items,
        page=PageInfo(
            limit=limit,
            offset=offset,
            total=None,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            fetched_urls=[search_response.fetched_url],
            source_response_id=_source_response_id("search", query),
            raw={
                "headers": search_response.headers,
                "request_body": body,
                "request_limit": request_limit,
                "returned_results": len(hits),
            },
        ),
        warnings=[],
    )


def handle_latest(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None), operation="latest")
    offset = _cursor_offset(cursor_payload)
    limit = int(getattr(args, "limit", None) or cursor_payload.get("limit") or DEFAULT_LIMIT)

    with _make_client() as client:
        latest_response = fetch_json(LATEST_CASES_URL, client=client)

    hits = _case_list(latest_response.payload, field=None, fetched_url=latest_response.fetched_url)
    selected_hits = hits[offset : offset + limit]
    items = [
        case_to_item(hit, fetched_url=latest_response.fetched_url, include_raw=bool(args.raw))
        for hit in selected_hits
    ]
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="latest",
        query={},
        items=items,
        page=page_info_from_offset(
            source=SOURCE_ID,
            operation="latest",
            offset=offset,
            limit=limit,
            total=len(hits),
            item_count=len(items),
        ),
        provenance=_provenance(
            fetched_urls=[latest_response.fetched_url],
            source_response_id="latest",
            raw={"headers": latest_response.headers, "returned_results": len(hits)},
        ),
        warnings=[],
    )


def handle_summary(args: argparse.Namespace) -> LegalResponse:
    fallo_id = _fallo_id_from_args(args, operation="summary")
    url = _summary_url(fallo_id)
    with _make_client() as client:
        summary_response = fetch_json(url, client=client)

    document = summary_payload_to_document(
        requested_fallo_id=fallo_id,
        url=url,
        summary_response=summary_response,
        include_raw=bool(args.raw),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="summary",
        request={"fallo_id": fallo_id},
        document=document,
        provenance=document.provenance,
    )


def handle_pdf(args: argparse.Namespace) -> LegalResponse:
    fallo_id = _fallo_id_from_args(args, operation="pdf")
    url = _pdf_url(fallo_id)
    with _make_client() as client:
        pdf_response = fetch_pdf_metadata(url, client=client)

    document = pdf_response_to_document(
        fallo_id=fallo_id,
        url=url,
        pdf_response=pdf_response,
        include_raw=bool(args.raw),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="pdf",
        request={"fallo_id": fallo_id},
        document=document,
        provenance=document.provenance,
    )


def fetch_json(url: str, *, client: LegalHttpClient | None = None) -> TfnJsonResponse:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", url)
        return TfnJsonResponse(
            payload=_json_payload(response, "TFN API response was not valid JSON"),
            fetched_url=str(response.url),
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def post_search(body: Mapping[str, Any], *, client: LegalHttpClient | None = None) -> TfnJsonResponse:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("POST", HYBRID_SEARCH_URL, json=dict(body))
        return TfnJsonResponse(
            payload=_json_payload(response, "TFN hybrid search response was not valid JSON"),
            fetched_url=str(response.url),
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def fetch_pdf_metadata(url: str, *, client: LegalHttpClient | None = None) -> TfnPdfResponse:
    owns_client = client is None
    http = client or _make_client()
    try:
        try:
            return TfnPdfResponse(response=http.head(url))
        except LegalCliError as exc:
            if _status_code(exc) not in {403, 405, 501}:
                raise
            response = http.request("GET", url, headers={"Range": "bytes=0-0"})
            return TfnPdfResponse(response=response, fallback_from=_error_evidence(exc))
    finally:
        if owns_client:
            http.close()


def parse_filters_payload(payload: Any, *, stats: Any | None = None) -> JsonDict:
    if not isinstance(payload, Mapping):
        raise parse_error(
            "TFN filters payload must be a JSON object",
            details={"payload_type": type(payload).__name__},
        )
    facets = {
        "tribunales": _facet_values(payload.get("tribunales"), normalize_case=True),
        "salas": _facet_values(payload.get("salas")),
        "vocalias": _facet_values(payload.get("vocalias")),
        "competencias": _facet_values(payload.get("competencias"), normalize_case=True),
    }
    if isinstance(stats, Mapping):
        facets["stats"] = dict(stats)
    return facets


def case_to_item(
    hit: Mapping[str, Any],
    *,
    fetched_url: str,
    include_raw: bool = False,
) -> LegalItem:
    metadata = _mapping(hit.get("metadata"))
    fallo_id = _fallo_id_from_hit(hit, metadata)
    file_url = _pdf_url(fallo_id)
    title = _first_text(metadata, "caratula", "registro", "expediente") or fallo_id
    date_value = normalize_date(_optional_text(metadata.get("fecha")))
    tribunal = _optional_text(metadata.get("tribunal"))
    sala = _optional_text(metadata.get("sala"))
    vocalia = _optional_int(metadata.get("vocalia"))
    competencia = _optional_text(metadata.get("competencia"))
    doctrinas = _doctrinas(hit.get("doctrinas"))
    matched_texto = _optional_text(hit.get("matched_texto"))
    objeto_texto = _optional_text(hit.get("objeto_texto"))
    search_in = _optional_text(hit.get("search_in"))
    match_source = _optional_text(hit.get("match_source"))

    return LegalItem(
        id=fallo_id,
        title=title,
        date=date_value,
        document_type=_document_type(metadata),
        url=file_url,
        file_url=file_url,
        snippet=clean_snippet(matched_texto or objeto_texto, max_length=SNIPPET_LENGTH),
        facets=_compact(
            {
                "tribunal": tribunal,
                "sala": sala,
                "vocalia": vocalia,
                "competencia": competencia,
                "regulacion_honorarios": _optional_bool(metadata.get("regulacion_honorarios")),
                "search_in": search_in,
                "match_source": match_source,
            }
        ),
        source_fields=_compact(
            {
                "fallo_id": fallo_id,
                "rank": _optional_int(hit.get("rank")),
                "search_in": search_in,
                "match_source": match_source,
                "matched_texto": matched_texto,
                "objeto_texto": objeto_texto,
                "metadata": metadata,
                "doctrinas": doctrinas,
                "pdf": {"url": file_url},
            }
        ),
        raw=dict(hit) if include_raw else {},
        provenance=_provenance(fetched_urls=[fetched_url], source_response_id=fallo_id),
    )


def summary_payload_to_document(
    *,
    requested_fallo_id: str,
    url: str,
    summary_response: TfnJsonResponse,
    include_raw: bool = False,
) -> LegalDocument:
    payload = summary_response.payload
    if not isinstance(payload, Mapping):
        raise parse_error(
            "TFN AI summary payload must be a JSON object",
            details={"payload_type": type(payload).__name__},
            provenance=_provenance(fetched_urls=[summary_response.fetched_url]),
        )
    fallo_id = _optional_text(payload.get("fallo_id")) or requested_fallo_id
    sections = {
        key: text
        for key in SUMMARY_FIELDS
        if (text := _optional_text(payload.get(key)))
    }
    body = "\n\n".join(f"{key}: {value}" for key, value in sections.items()) or None
    document_id = f"{SOURCE_ID}:summary:{fallo_id}"
    return LegalDocument(
        id=document_id,
        title=f"TFN AI summary {fallo_id}",
        document_type="ai_summary",
        body=body,
        url=url,
        content_type="application/json",
        text_format="plain",
        metadata=_compact(
            {
                "fallo_id": fallo_id,
                "summary_fields": list(sections.keys()),
                "field_count": len(sections),
            }
        ),
        source_fields=_compact(
            {
                "fallo_id": fallo_id,
                "summary_endpoint": url,
                "summary": sections,
            }
        ),
        raw=dict(payload) if include_raw else {},
        provenance=_provenance(
            fetched_urls=[summary_response.fetched_url],
            source_response_id=document_id,
            raw={"headers": summary_response.headers},
        ),
    )


def pdf_response_to_document(
    *,
    fallo_id: str,
    url: str,
    pdf_response: TfnPdfResponse,
    include_raw: bool = False,
) -> LegalDocument:
    response = pdf_response.response
    content_type = _optional_text(response.headers.get("content-type"))
    content_length = _content_length(response.headers)
    content_disposition = _optional_text(response.headers.get("content-disposition"))
    disposition_type, disposition_filename = _content_disposition_parts(content_disposition)
    filename = disposition_filename or f"{fallo_id}.pdf"
    kind = _attachment_kind(url=url, content_type=content_type, filename=filename)
    document_id = f"{SOURCE_ID}:pdf:{fallo_id}"
    headers = _useful_headers(response)
    file_entry = _compact(
        {
            "url": url,
            "label": filename,
            "kind": kind,
            "content_type": content_type,
            "content_length": content_length,
            "content_disposition": content_disposition,
            "disposition_type": disposition_type,
        }
    )
    metadata = _compact(
        {
            "fallo_id": fallo_id,
            "filename": filename,
            "extension": _extension(filename),
            "kind": kind,
            "content_type": content_type,
            "content_length": content_length,
            "response_content_length": _header_content_length(response.headers),
            "content_range": _optional_text(response.headers.get("content-range")),
            "content_disposition": content_disposition,
            "disposition_type": disposition_type,
            "last_modified": _optional_text(response.headers.get("last-modified")),
            "etag": _optional_text(response.headers.get("etag")),
            "accept_ranges": _optional_text(response.headers.get("accept-ranges")),
            "method": response.request.method,
            "status_code": response.status_code,
        }
    )
    return LegalDocument(
        id=document_id,
        title=filename,
        document_type=kind,
        url=url,
        file_url=url,
        content_type=content_type,
        metadata=metadata,
        links=[{"url": url, "label": filename, "kind": kind}],
        files=[file_entry],
        source_fields={"fallo_id": fallo_id, "pdf_endpoint": url},
        raw={"headers": headers} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=document_id,
            raw=_compact(
                {
                    "method": response.request.method,
                    "status_code": response.status_code,
                    "headers": headers,
                    "fallback_from": pdf_response.fallback_from,
                }
            ),
        ),
    )


def build_search_query(
    *,
    query: str | None = None,
    search_in: str | None = None,
    tribunales: Sequence[str] | None = None,
    registro: str | None = None,
    expediente: str | None = None,
    caratula: str | None = None,
    salas: Sequence[str] | None = None,
    vocalias: Sequence[str] | None = None,
    competencias: Sequence[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    honorarios: bool = False,
) -> JsonDict:
    normalized_query = _optional_text(query)
    normalized_search_in = _choice(search_in or DEFAULT_SEARCH_IN, allowed=SEARCH_IN_VALUES, field="search-in")
    normalized_tribunales = _choice_list(tribunales, allowed=TRIBUNAL_VALUES, field="tribunal")
    normalized_registro = _optional_text(registro)
    normalized_expediente = _optional_text(expediente)
    normalized_caratula = _optional_text(caratula)
    normalized_salas = [_require_text(value, field="sala") for value in (salas or [])]
    normalized_vocalias = [_vocalia(value) for value in (vocalias or [])]
    normalized_competencias = _choice_list(competencias, allowed=COMPETENCIA_VALUES, field="competencia")
    iso_from = _iso_date(date_from, field="from")
    iso_to = _iso_date(date_to, field="to")
    if iso_from and iso_to and date.fromisoformat(iso_from) > date.fromisoformat(iso_to):
        raise usage_error("--from must be less than or equal to --to")

    has_filter = any(
        [
            normalized_query,
            normalized_tribunales,
            normalized_registro,
            normalized_expediente,
            normalized_caratula,
            normalized_salas,
            normalized_vocalias,
            normalized_competencias,
            iso_from,
            iso_to,
            bool(honorarios),
        ]
    )
    if not has_filter:
        raise usage_error("tfn search requires --query or at least one source-specific filter")

    return {
        "query": normalized_query or "",
        "search_in": normalized_search_in,
        "tribunales": normalized_tribunales,
        "registro": normalized_registro or "",
        "expediente": normalized_expediente or "",
        "caratula": normalized_caratula or "",
        "salas": normalized_salas,
        "vocalias": normalized_vocalias,
        "competencias": normalized_competencias,
        "fecha_desde": iso_from,
        "fecha_hasta": iso_to,
        "regulacion_honorarios": bool(honorarios),
    }


def search_body(query: Mapping[str, Any], *, limit: int) -> JsonDict:
    return {
        "query": _optional_text(query.get("query")) or "",
        "search_in": _optional_text(query.get("search_in")) or DEFAULT_SEARCH_IN,
        "tribunales": list(query.get("tribunales") or []),
        "registro": _optional_text(query.get("registro")) or "",
        "expediente": _optional_text(query.get("expediente")) or "",
        "caratula": _optional_text(query.get("caratula")) or "",
        "salas": list(query.get("salas") or []),
        "vocalias": list(query.get("vocalias") or []),
        "competencias": list(query.get("competencias") or []),
        "fecha_desde": _optional_text(query.get("fecha_desde")),
        "fecha_hasta": _optional_text(query.get("fecha_hasta")),
        "regulacion_honorarios": bool(query.get("regulacion_honorarios")),
        "limit": limit,
    }


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"source {SOURCE_ID!r} is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("filters", handle_filters, help="return TFN API filter values", add_arguments=None)
    adapter.register_operation("search", handle_search, help="search TFN jurisprudence", add_arguments=add_search_arguments)
    adapter.register_operation("latest", handle_latest, help="list latest TFN API cases", add_arguments=add_latest_arguments)
    adapter.register_operation("summary", handle_summary, help="fetch TFN AI summary metadata", add_arguments=add_summary_arguments)
    adapter.register_operation("pdf", handle_pdf, help="inspect TFN PDF metadata", add_arguments=add_pdf_arguments)
    return adapter


def _add_fallo_id_arguments(parser: argparse.ArgumentParser, operation: str) -> None:
    parser.add_argument("fallo_id", nargs="?", help=f"TFN fallo_id for {operation}")
    parser.add_argument("--fallo-id", dest="fallo_id_option", help=f"TFN fallo_id for {operation}")
    parser.add_argument("--id", dest="id", help=f"TFN fallo_id for {operation}")


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    if cursor_payload and not _has_explicit_search_args(args):
        query = _query_from_cursor(cursor_payload)
        if query is not None:
            return query
    return build_search_query(
        query=getattr(args, "query", None),
        search_in=getattr(args, "search_in", None),
        tribunales=getattr(args, "tribunal", None),
        registro=getattr(args, "registro", None),
        expediente=getattr(args, "expediente", None),
        caratula=getattr(args, "caratula", None),
        salas=getattr(args, "sala", None),
        vocalias=getattr(args, "vocalia", None),
        competencias=getattr(args, "competencia", None),
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
        honorarios=bool(getattr(args, "honorarios", False)),
    )


def _query_from_cursor(cursor_payload: Mapping[str, Any]) -> JsonDict | None:
    raw = cursor_payload.get("raw")
    if not isinstance(raw, Mapping):
        return None
    query = raw.get("query")
    return dict(query) if isinstance(query, Mapping) else None


def _has_explicit_search_args(args: argparse.Namespace) -> bool:
    text_fields = ("query", "registro", "expediente", "caratula", "date_from", "date_to")
    list_fields = ("tribunal", "sala", "vocalia", "competencia")
    if any(_optional_text(getattr(args, name, None)) for name in text_fields):
        return True
    if any(bool(getattr(args, name, None)) for name in list_fields):
        return True
    if bool(getattr(args, "honorarios", False)):
        return True
    search_in = _optional_text(getattr(args, "search_in", None))
    return bool(search_in and search_in != DEFAULT_SEARCH_IN)


def _fallo_id_from_args(args: argparse.Namespace, *, operation: str) -> str:
    value = (
        _optional_text(getattr(args, "fallo_id_option", None))
        or _optional_text(getattr(args, "id", None))
        or _optional_text(getattr(args, "fallo_id", None))
    )
    if not value:
        raise usage_error(f"{operation} requires --fallo-id", details={"source": SOURCE_ID, "operation": operation})
    return value


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _cursor_offset(cursor_payload: Mapping[str, Any]) -> int:
    offset = cursor_payload.get("offset")
    return offset if isinstance(offset, int) and offset >= 0 else 0


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"status_code": response.status_code}),
        ) from exc


def _case_list(payload: Any, *, field: str | None, fetched_url: str) -> list[JsonDict]:
    raw_hits = payload.get(field) if field and isinstance(payload, Mapping) else payload
    if not isinstance(raw_hits, list):
        raise parse_error(
            "TFN case payload is missing a result list",
            details={"payload_type": type(payload).__name__, "field": field},
            provenance=_provenance(fetched_urls=[fetched_url]),
        )
    hits: list[JsonDict] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "TFN case entries must be JSON objects",
                details={"entry_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[fetched_url]),
            )
        hits.append(dict(hit))
    return hits


def _facet_values(value: Any, *, normalize_case: bool = False) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    facets: list[JsonDict] = []
    for item in value:
        label = _optional_text(item)
        if label is None:
            continue
        facets.append(
            _compact(
                {
                    "value": label.casefold() if normalize_case and isinstance(item, str) else label,
                    "label": label,
                    "id": item,
                }
            )
        )
    return facets


def _fallo_id_from_hit(hit: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    fallo_id = _optional_text(hit.get("fallo_id")) or _optional_text(metadata.get("fallo_id"))
    if fallo_id is None:
        raise parse_error("TFN search result is missing fallo_id")
    return fallo_id


def _document_type(metadata: Mapping[str, Any]) -> str:
    tribunal = _optional_text(metadata.get("tribunal"))
    return f"{tribunal}_fallo" if tribunal else "fallo"


def _doctrinas(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    doctrinas: list[JsonDict] = []
    for item in value:
        if isinstance(item, Mapping):
            doctrinas.append(
                _compact(
                    {
                        "id": item.get("id"),
                        "texto": _optional_text(item.get("texto")),
                    }
                )
            )
            continue
        text = _optional_text(item)
        if text:
            doctrinas.append({"texto": text})
    return doctrinas


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _response_query(query: Mapping[str, Any], *, limit: int, offset: int) -> JsonDict:
    response = {
        key: item
        for key, item in query.items()
        if item is not None and item is not False and item != "" and item != [] and item != {}
    }
    response["limit"] = limit
    response["offset"] = offset
    return response


def _source_response_id(operation: str, query: Mapping[str, Any]) -> str:
    parts = [
        operation,
        _optional_text(query.get("query")) or "empty",
        _optional_text(query.get("search_in")) or DEFAULT_SEARCH_IN,
    ]
    tribunales = query.get("tribunales")
    if isinstance(tribunales, list) and tribunales:
        parts.append("+".join(str(item) for item in tribunales))
    return ":".join(parts)[:160]


def _summary_url(fallo_id: str) -> str:
    return SUMMARY_URL_TEMPLATE.format(fallo_id=quote(fallo_id, safe=""))


def _pdf_url(fallo_id: str) -> str:
    return PDF_URL_TEMPLATE.format(fallo_id=quote(fallo_id, safe=""))


def _choice(value: str, *, allowed: set[str], field: str) -> str:
    text = _require_text(value, field=field).casefold()
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise usage_error(f"--{field} must be one of {choices}")
    return text


def _choice_list(values: Sequence[str] | None, *, allowed: set[str], field: str) -> list[str]:
    return [_choice(value, allowed=allowed, field=field) for value in (values or [])]


def _require_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise usage_error(f"--{field} cannot be empty")
    return text


def _vocalia(value: str) -> int:
    text = _require_text(value, field="vocalia")
    if not text.isdigit():
        raise usage_error("--vocalia must be an integer from 1 through 21")
    parsed = int(text)
    if parsed < 1 or parsed > 21:
        raise usage_error("--vocalia must be an integer from 1 through 21")
    return parsed


def _iso_date(value: str | None, *, field: str) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise usage_error(f"--{field} must be an ISO date YYYY-MM-DD") from exc


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _content_disposition_parts(value: str | None) -> tuple[str | None, str | None]:
    text = _optional_text(value)
    if text is None:
        return None, None
    message = Message()
    message["content-disposition"] = text
    return message.get_content_disposition(), _optional_text(message.get_filename())


def _content_length(headers: httpx.Headers) -> int | None:
    content_range = headers.get("content-range")
    if content_range:
        match = _CONTENT_RANGE_TOTAL_RE.search(content_range)
        if match:
            return int(match.group("total"))
    return _header_content_length(headers)


def _header_content_length(headers: httpx.Headers) -> int | None:
    raw_length = headers.get("content-length")
    if raw_length and raw_length.isdigit():
        return int(raw_length)
    return None


def _extension(filename: str | None) -> str | None:
    text = _optional_text(filename)
    if not text or "." not in text:
        return None
    return text.rsplit(".", 1)[-1].lower()


def _attachment_kind(*, url: str, content_type: str | None, filename: str | None) -> str:
    kind = classify_link(filename or _filename_from_url(url) or url, base_url=API_BASE_URL, content_type=content_type)
    return "file" if kind in {"page", "relative", "unknown"} else kind


def _filename_from_url(url: str | None) -> str | None:
    text = _optional_text(url)
    if not text:
        return None
    path = urlparse(text).path
    return _optional_text(unquote(path.rsplit("/", 1)[-1]))


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _optional_text(mapping.get(key))
        if text:
            return text
    return None


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


def _useful_headers(response: httpx.Response) -> JsonDict:
    allowed = {
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
        "location",
        "retry-after",
    }
    return {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in allowed
    }


def _status_code(error: LegalCliError) -> int | None:
    details = error.details or {}
    status = details.get("status_code")
    return status if isinstance(status, int) else None


def _error_evidence(error: LegalCliError) -> JsonDict:
    details = error.details or {}
    headers = details.get("headers")
    return _compact(
        {
            "method": details.get("method"),
            "url": details.get("url"),
            "status_code": details.get("status_code"),
            "reason_phrase": details.get("reason_phrase"),
            "headers": headers if isinstance(headers, Mapping) else None,
        }
    )


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[HUMAN_URL, API_BASE_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Accept": "application/json, text/plain, */*"})


register_adapter(build_adapter(), replace=True)
