"""CNACAF jurisprudence adapter backed by TFN/CNCAF and PJN APIs."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import Any
from urllib.parse import quote

from legal.errors import parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import decode_cursor, make_cursor
from legal.parsing import clean_text
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter
from legal.sources import pjn_juris, tfn


SOURCE_ID = "cnacaf"
SOURCE_MAP = "apps/legal/docs/cnacaf_jurisprudencia.md"

HUMAN_URL = "https://jurisprudenciatfn.mecon.gob.ar/"
API_BASE_URL = tfn.API_BASE_URL
PJN_HUMAN_URL = pjn_juris.HUMAN_URL
PJN_API_BASE_URL = pjn_juris.API_BASE_URL

DEFAULT_LIMIT = tfn.DEFAULT_LIMIT
DEFAULT_SEARCH_IN = tfn.DEFAULT_SEARCH_IN
SEARCH_IN_VALUES = tfn.SEARCH_IN_VALUES
DEFAULT_TRIBUNAL = "cncaf"
TFN_CAF_BACKEND = "tfn-cncaf-api"
PJN_FALLBACK_BACKEND = "pjn-documento-api"
CAF_SALA_RE = re.compile(r"^Sala\s+(?P<roman>[IVXLCDM]+|\d+)\s*\(CAF\)$", re.IGNORECASE)

PJN_FALLBACK_SCOPE_WARNING = (
    "PJN fallback results come from pjn-documento-api and may include non-CNACAF records "
    "unless dependency/rubro filters narrow the search"
)


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", "--text", "--q", dest="query", help="free text query")
    parser.add_argument(
        "--search-in",
        default=DEFAULT_SEARCH_IN,
        help="TFN/CNCAF hybrid search field: objetos or doctrinas",
    )
    parser.add_argument("--tribunal", action="append", help="tribunal filter; only cncaf is accepted")
    parser.add_argument("--registro", help="registro filter")
    parser.add_argument("--expediente", help="expediente filter")
    parser.add_argument("--caratula", help="caratula filter")
    parser.add_argument("--sala", action="append", help='CAF sala filter; repeatable, e.g. "Sala V (CAF)"')
    parser.add_argument("--from", dest="date_from", help="decision date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="decision date upper bound, YYYY-MM-DD")
    parser.add_argument("--honorarios", action="store_true", help="filter cases with fee regulation")


def add_pdf_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("fallo_id", nargs="?", help="CNACAF fallo_id for PDF metadata")
    parser.add_argument("--fallo-id", dest="fallo_id_option", help="CNACAF fallo_id for PDF metadata")
    parser.add_argument("--id", dest="id", help="CNACAF fallo_id for PDF metadata")


def add_pjn_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--terms", "--text", "--q", dest="terms", help="free text terms")
    parser.add_argument("--dependencia", help="PJN dependency facet id")
    parser.add_argument("--rubro", help="PJN rubro facet id")
    parser.add_argument("--subrubro", help="PJN subrubro facet id")
    parser.add_argument("--from", dest="date_from", help="publication date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="publication date upper bound, YYYY-MM-DD")
    parser.add_argument("--number", help="document number filter")
    parser.add_argument("--year", help="document year filter")
    parser.add_argument("--sort", help="PJN sort alias or API value")
    parser.add_argument("--page", type=_non_negative_int, help="zero-based PJN result page")
    parser.add_argument(
        "--allow-broad-search",
        "--allow-empty-search",
        action="store_true",
        help="allow empty portal-wide PJN document API queries",
    )


def handle_filters(args: argparse.Namespace) -> LegalResponse:
    with _make_client() as client:
        filters = tfn.fetch_json(tfn.FILTERS_URL, client=client)
        stats = tfn.fetch_json(tfn.SEARCH_STATS_URL, client=client)

    facets = _cnacaf_facets(tfn.parse_filters_payload(filters.payload, stats=stats.payload))
    raw: JsonDict = {
        "backend": TFN_CAF_BACKEND,
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
        query={"tribunales": [DEFAULT_TRIBUNAL]},
        facets=facets,
        provenance=_provenance(
            fetched_urls=[filters.fetched_url, stats.fetched_url],
            source_response_id="filters:cncaf",
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
    body = tfn.search_body(query, limit=request_limit)

    with _make_client() as client:
        search_response = tfn.post_search(body, client=client)

    hits = _case_list(search_response.payload, fetched_url=search_response.fetched_url)
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
                "backend": TFN_CAF_BACKEND,
                "headers": search_response.headers,
                "request_body": body,
                "request_limit": request_limit,
                "returned_results": len(hits),
            },
        ),
        warnings=[],
    )


def handle_pdf(args: argparse.Namespace) -> LegalResponse:
    fallo_id = _fallo_id_from_args(args, operation="pdf")
    url = _pdf_url(fallo_id)
    with _make_client() as client:
        pdf_response = tfn.fetch_pdf_metadata(url, client=client)

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


def handle_pjn_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None), operation="pjn-search")
    query = _pjn_query_from_args(args, cursor_payload=cursor_payload)
    if not query["query_string"] and not bool(query.get("allow_broad_search")):
        raise usage_error(
            "empty PJN fallback search is portal-wide; pass --allow-broad-search to run it explicitly",
            details={"source": SOURCE_ID, "operation": "pjn-search"},
        )

    limit = int(getattr(args, "limit", None) or cursor_payload.get("limit") or pjn_juris.DEFAULT_LIMIT)
    requested_page = _requested_pjn_page(args, cursor_payload)
    offset = _cursor_offset(cursor_payload)
    warnings = _pjn_warnings(query)

    with _make_client() as client:
        search_page = pjn_juris.fetch_search_page(
            query_string=query["query_string"],
            sort=_optional_text(query.get("sort")),
            page=requested_page,
            client=client,
        )

    page_offset = min(offset, len(search_page.hits))
    selected_hits = search_page.hits[page_offset : page_offset + limit]
    items = [
        pjn_hit_to_item(hit, search_page=search_page, include_raw=bool(args.raw))
        for hit in selected_hits
    ]
    next_cursor = _pjn_next_cursor(
        search_page=search_page,
        query=query,
        limit=limit,
        offset=page_offset,
        returned_count=len(items),
    )
    page_size = search_page.size or len(search_page.hits)
    global_offset = (search_page.number * page_size) + page_offset

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="pjn-search",
        query=_pjn_response_query(query, page=search_page.number, limit=limit, offset=global_offset),
        items=items,
        page=PageInfo(
            limit=limit,
            offset=global_offset,
            page=search_page.number,
            total=search_page.total,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=_pjn_source_response_id(query, search_page.number),
            raw={
                "backend": PJN_FALLBACK_BACKEND,
                "primary_backend": TFN_CAF_BACKEND,
                "headers": search_page.headers,
                "spring_page": _spring_page_metadata(search_page.payload),
                "backend_page_size": page_size,
                "backend_offset": page_offset,
            },
        ),
        warnings=warnings,
    )


def case_to_item(
    hit: Mapping[str, Any],
    *,
    fetched_url: str,
    include_raw: bool = False,
) -> LegalItem:
    base = tfn.case_to_item(hit, fetched_url=fetched_url, include_raw=include_raw)
    metadata = _mapping(hit.get("metadata"))
    sala_fields = _caf_sala_fields(_optional_text(metadata.get("sala")))
    source_fields = _compact(
        {
            **base.source_fields,
            "backend": TFN_CAF_BACKEND,
            "source": SOURCE_ID,
            "api": API_BASE_URL,
            "tribunal": DEFAULT_TRIBUNAL,
            "caf_sala": sala_fields,
        }
    )
    facets = _compact(
        {
            **base.facets,
            "tribunal": DEFAULT_TRIBUNAL,
            "backend": TFN_CAF_BACKEND,
            "sala_court": sala_fields.get("court"),
            "sala_roman": sala_fields.get("roman"),
            "sala_number": sala_fields.get("number"),
        }
    )
    return replace(
        base,
        facets=facets,
        source_fields=source_fields,
        provenance=_provenance(fetched_urls=[fetched_url], source_response_id=base.id),
    )


def pdf_response_to_document(
    *,
    fallo_id: str,
    url: str,
    pdf_response: tfn.TfnPdfResponse,
    include_raw: bool = False,
) -> LegalDocument:
    base = tfn.pdf_response_to_document(
        fallo_id=fallo_id,
        url=url,
        pdf_response=pdf_response,
        include_raw=include_raw,
    )
    source_fields = _compact(
        {
            **base.source_fields,
            "backend": TFN_CAF_BACKEND,
            "source": SOURCE_ID,
            "api": API_BASE_URL,
            "tribunal": DEFAULT_TRIBUNAL,
        }
    )
    metadata = _compact(
        {
            **base.metadata,
            "backend": TFN_CAF_BACKEND,
            "tribunal": DEFAULT_TRIBUNAL,
        }
    )
    return replace(
        base,
        id=f"{SOURCE_ID}:pdf:{fallo_id}",
        metadata=metadata,
        source_fields=source_fields,
        provenance=_provenance(
            fetched_urls=base.provenance.fetched_urls if base.provenance else [url],
            source_response_id=f"{SOURCE_ID}:pdf:{fallo_id}",
            raw=base.provenance.raw if base.provenance else {},
        ),
    )


def pjn_hit_to_item(
    hit: Mapping[str, Any],
    *,
    search_page: pjn_juris.PjnSearchPage,
    include_raw: bool = False,
) -> LegalItem:
    base = pjn_juris.hit_to_item(hit, search_page=search_page, include_raw=include_raw)
    document_id = _pjn_document_id(base)
    source_fields = _compact(
        {
            **base.source_fields,
            "backend": PJN_FALLBACK_BACKEND,
            "fallback": True,
            "fallback_source": "pjn",
            "primary_backend": TFN_CAF_BACKEND,
            "source": SOURCE_ID,
            "pjn_document_id": document_id,
        }
    )
    facets = _compact({**base.facets, "fallback_source": "pjn", "backend": PJN_FALLBACK_BACKEND})
    return replace(
        base,
        id=f"{SOURCE_ID}:pjn:{document_id}",
        facets=facets,
        source_fields=source_fields,
        provenance=_provenance(fetched_urls=[search_page.fetched_url], source_response_id=f"pjn:{document_id}"),
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
    date_from: str | None = None,
    date_to: str | None = None,
    honorarios: bool = False,
) -> JsonDict:
    normalized_query = _optional_text(query)
    normalized_search_in = _choice(search_in or DEFAULT_SEARCH_IN, allowed=SEARCH_IN_VALUES, field="search-in")
    normalized_tribunales = _cnacaf_tribunales(tribunales)
    normalized_registro = _optional_text(registro)
    normalized_expediente = _optional_text(expediente)
    normalized_caratula = _optional_text(caratula)
    normalized_salas = [_caf_sala(value) for value in (salas or [])]
    iso_from = _iso_date(date_from, field="from")
    iso_to = _iso_date(date_to, field="to")
    if iso_from and iso_to and date.fromisoformat(iso_from) > date.fromisoformat(iso_to):
        raise usage_error("--from must be less than or equal to --to")

    has_filter = any(
        [
            normalized_query,
            normalized_registro,
            normalized_expediente,
            normalized_caratula,
            normalized_salas,
            iso_from,
            iso_to,
            bool(honorarios),
        ]
    )
    if not has_filter:
        raise usage_error("cnacaf search requires --query or at least one CNACAF filter")

    return {
        "query": normalized_query or "",
        "search_in": normalized_search_in,
        "tribunales": normalized_tribunales,
        "registro": normalized_registro or "",
        "expediente": normalized_expediente or "",
        "caratula": normalized_caratula or "",
        "salas": normalized_salas,
        "vocalias": [],
        "competencias": [],
        "fecha_desde": iso_from,
        "fecha_hasta": iso_to,
        "regulacion_honorarios": bool(honorarios),
    }


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"source {SOURCE_ID!r} is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("filters", handle_filters, help="return CNACAF filter values", add_arguments=None)
    adapter.register_operation("search", handle_search, help="search CNACAF through the TFN/CNCAF API", add_arguments=add_search_arguments)
    adapter.register_operation("pdf", handle_pdf, help="inspect CNACAF PDF metadata", add_arguments=add_pdf_arguments)
    adapter.register_operation("pjn-search", handle_pjn_search, help="search PJN document fallback", add_arguments=add_pjn_search_arguments)
    return adapter


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
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
        honorarios=bool(getattr(args, "honorarios", False)),
    )


def _pjn_query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    if cursor_payload and not _has_explicit_pjn_query_args(args):
        query = _query_from_cursor(cursor_payload)
        if query is not None:
            return query
    return pjn_juris.build_query(
        terms=getattr(args, "terms", None),
        dependencia=getattr(args, "dependencia", None),
        rubro=getattr(args, "rubro", None),
        subrubro=getattr(args, "subrubro", None),
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
        number=getattr(args, "number", None),
        year=getattr(args, "year", None),
        sort=getattr(args, "sort", None),
        allow_broad_search=bool(getattr(args, "allow_broad_search", False)),
    )


def _query_from_cursor(cursor_payload: Mapping[str, Any]) -> JsonDict | None:
    raw = cursor_payload.get("raw")
    if not isinstance(raw, Mapping):
        return None
    query = raw.get("query")
    return dict(query) if isinstance(query, Mapping) else None


def _has_explicit_search_args(args: argparse.Namespace) -> bool:
    text_fields = ("query", "registro", "expediente", "caratula", "date_from", "date_to")
    list_fields = ("tribunal", "sala")
    if any(_optional_text(getattr(args, name, None)) for name in text_fields):
        return True
    if any(bool(getattr(args, name, None)) for name in list_fields):
        return True
    if bool(getattr(args, "honorarios", False)):
        return True
    search_in = _optional_text(getattr(args, "search_in", None))
    return bool(search_in and search_in != DEFAULT_SEARCH_IN)


def _has_explicit_pjn_query_args(args: argparse.Namespace) -> bool:
    fields = ("terms", "dependencia", "rubro", "subrubro", "date_from", "date_to", "number", "year", "sort")
    return any(_optional_text(getattr(args, name, None)) for name in fields) or bool(
        getattr(args, "allow_broad_search", False)
    )


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


def _requested_pjn_page(args: argparse.Namespace, cursor_payload: Mapping[str, Any]) -> int:
    page_arg = getattr(args, "page", None)
    if page_arg is not None:
        return int(page_arg)
    page = cursor_payload.get("page")
    if isinstance(page, int) and page >= 0:
        return page
    return pjn_juris.DEFAULT_PAGE


def _pjn_next_cursor(
    *,
    search_page: pjn_juris.PjnSearchPage,
    query: Mapping[str, Any],
    limit: int,
    offset: int,
    returned_count: int,
) -> str | None:
    if returned_count <= 0:
        return None
    next_offset = offset + returned_count
    if next_offset < len(search_page.hits):
        next_page = search_page.number
    elif _has_next_pjn_backend_page(search_page):
        next_page = search_page.number + 1
        next_offset = 0
    else:
        return None
    return make_cursor(
        source=SOURCE_ID,
        operation="pjn-search",
        page=next_page,
        offset=next_offset,
        limit=limit,
        raw={"query": dict(query)},
    )


def _has_next_pjn_backend_page(search_page: pjn_juris.PjnSearchPage) -> bool:
    last = search_page.payload.get("last")
    if isinstance(last, bool):
        return not last
    total_pages = _optional_int(search_page.payload.get("totalPages"))
    if total_pages is not None:
        return search_page.number + 1 < total_pages
    if search_page.total is not None and search_page.size:
        return (search_page.number + 1) * search_page.size < search_page.total
    return False


def _case_list(payload: Any, *, fetched_url: str) -> list[JsonDict]:
    raw_hits = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(raw_hits, list):
        raise parse_error(
            "CNACAF search payload is missing results",
            details={"payload_type": type(payload).__name__},
            provenance=_provenance(fetched_urls=[fetched_url]),
        )
    hits: list[JsonDict] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "CNACAF search result entries must be JSON objects",
                details={"entry_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[fetched_url]),
            )
        hits.append(dict(hit))
    return hits


def _cnacaf_facets(facets: Mapping[str, Any]) -> JsonDict:
    tribunales = [
        item
        for item in _list_of_mappings(facets.get("tribunales"))
        if _optional_text(item.get("value")) == DEFAULT_TRIBUNAL
    ]
    salas = [
        _compact({**item, "caf": _caf_sala_fields(_optional_text(item.get("label")) or _optional_text(item.get("value")))})
        for item in _list_of_mappings(facets.get("salas"))
        if _is_caf_sala(_optional_text(item.get("label")) or _optional_text(item.get("value")))
    ]
    result = {
        "tribunales": tribunales or [{"value": DEFAULT_TRIBUNAL, "label": DEFAULT_TRIBUNAL, "id": DEFAULT_TRIBUNAL}],
        "salas": salas,
        "caf_salas": [item["caf"] for item in salas if isinstance(item.get("caf"), Mapping)],
    }
    if isinstance(facets.get("stats"), Mapping):
        result["stats"] = dict(facets["stats"])
    return result


def _list_of_mappings(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _cnacaf_tribunales(values: Sequence[str] | None) -> list[str]:
    if not values:
        return [DEFAULT_TRIBUNAL]
    normalized: list[str] = []
    for value in values:
        text = _require_text(value, field="tribunal").casefold()
        if text != DEFAULT_TRIBUNAL:
            raise usage_error("--tribunal for cnacaf must be cncaf")
        if text not in normalized:
            normalized.append(text)
    return normalized or [DEFAULT_TRIBUNAL]


def _caf_sala(value: str) -> str:
    text = _require_text(value, field="sala")
    if not _is_caf_sala(text):
        raise usage_error('--sala must be a CAF sala such as "Sala V (CAF)"')
    return text


def _caf_sala_fields(value: str | None) -> JsonDict:
    if not value:
        return {}
    match = CAF_SALA_RE.match(value)
    if not match:
        return {"label": value}
    raw_number = match.group("roman").upper()
    return _compact(
        {
            "label": value,
            "court": "CAF",
            "roman": raw_number if not raw_number.isdigit() else None,
            "number": _roman_to_int(raw_number) if not raw_number.isdigit() else int(raw_number),
        }
    )


def _is_caf_sala(value: str | None) -> bool:
    return bool(value and CAF_SALA_RE.match(value))


def _roman_to_int(value: str) -> int | None:
    numerals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    try:
        for char in reversed(value.upper()):
            current = numerals[char]
            if current < previous:
                total -= current
            else:
                total += current
                previous = current
    except KeyError:
        return None
    return total or None


def _response_query(query: Mapping[str, Any], *, limit: int, offset: int) -> JsonDict:
    response = {
        key: item
        for key, item in query.items()
        if item is not None and item is not False and item != "" and item != [] and item != {}
    }
    response["limit"] = limit
    response["offset"] = offset
    response["backend"] = TFN_CAF_BACKEND
    return response


def _pjn_response_query(
    query: Mapping[str, Any],
    *,
    page: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> JsonDict:
    response = {key: value for key, value in query.items() if value is not None}
    response["backend"] = PJN_FALLBACK_BACKEND
    if page is not None:
        response["page"] = page
    if limit is not None:
        response["limit"] = limit
    if offset is not None:
        response["offset"] = offset
    return response


def _pjn_warnings(query: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not _optional_text(query.get("query_string")):
        warnings.append(pjn_juris.BROAD_SEARCH_WARNING)
    if not any(_optional_text(query.get(key)) for key in ("dependencia", "rubro", "subrubro")):
        warnings.append(PJN_FALLBACK_SCOPE_WARNING)
    return warnings


def _source_response_id(operation: str, query: Mapping[str, Any]) -> str:
    parts = [
        operation,
        _optional_text(query.get("query")) or "empty",
        _optional_text(query.get("search_in")) or DEFAULT_SEARCH_IN,
        DEFAULT_TRIBUNAL,
    ]
    return ":".join(parts)[:160]


def _pjn_source_response_id(query: Mapping[str, Any], page: int | None) -> str:
    page_part = "" if page is None else f":{page}"
    query_string = _optional_text(query.get("query_string")) or "empty"
    return f"pjn-search{page_part}:{query_string[:120]}"


def _spring_page_metadata(payload: Mapping[str, Any]) -> JsonDict:
    return {
        key: value
        for key, value in payload.items()
        if key
        in {
            "totalElements",
            "totalPages",
            "size",
            "number",
            "numberOfElements",
            "first",
            "last",
            "empty",
            "sort",
            "pageable",
        }
    }


def _pdf_url(fallo_id: str) -> str:
    return tfn.PDF_URL_TEMPLATE.format(fallo_id=quote(fallo_id, safe=""))


def _pjn_document_id(item: LegalItem) -> str:
    document_id = _optional_text(item.source_fields.get("document_id"))
    if document_id:
        return document_id
    return item.id.rsplit(":", 1)[-1]


def _choice(value: str, *, allowed: set[str], field: str) -> str:
    text = _require_text(value, field=field).casefold()
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise usage_error(f"--{field} must be one of {choices}")
    return text


def _require_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise usage_error(f"--{field} cannot be empty")
    return text


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


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[HUMAN_URL, API_BASE_URL, PJN_HUMAN_URL, PJN_API_BASE_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Accept": "application/json, text/plain, */*"})


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


register_adapter(build_adapter(), replace=True)
