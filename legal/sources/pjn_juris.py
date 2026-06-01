"""PJN document API adapter for jurisprudence-oriented searches."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from apps.legal.errors import LegalCliError, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from apps.legal.pagination import decode_cursor, make_cursor
from apps.legal.parsing import classify_link, clean_snippet, clean_text, normalize_date
from apps.legal.registry import get_source
from apps.legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "pjn-juris"
SOURCE_MAP = "apps/legal/docs/pjn_jurisprudencia.md"

API_BASE_URL = "https://pjn-documento-api.pjn.gov.ar"
HUMAN_URL = "https://www.pjn.gov.ar/jurisprudencia2/consulta.php"
SEARCH_URL = f"{API_BASE_URL}/api/documento/search"
FACETS_URL = f"{API_BASE_URL}/api/documento/search/filter"
ATTACHMENT_URL_TEMPLATE = f"{API_BASE_URL}/api/documento/adjunto/{{document_id}}"

DEFAULT_LIMIT = 10
DEFAULT_PAGE = 0
DEFAULT_SORT = "desde,desc"
SNIPPET_LENGTH = 360
BROAD_SEARCH_WARNING = (
    "empty PJN document API searches are portal-wide, not jurisprudence-only; "
    "use terms or dependency/rubro filters unless that broader scope is intended"
)
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/(?P<total>\d+)\s*$")

SORT_ALIASES: Mapping[str, str] = {
    "recent": "desde,desc",
    "desde-desc": "desde,desc",
    "desde,desc": "desde,desc",
    "oldest": "desde,asc",
    "desde-asc": "desde,asc",
    "desde,asc": "desde,asc",
    "order-desc": "orden,desc",
    "orden-desc": "orden,desc",
    "orden,desc": "orden,desc",
    "order-asc": "orden,asc",
    "orden-asc": "orden,asc",
    "orden,asc": "orden,asc",
}
SORT_CHOICES = ("recent", "oldest", "order-desc", "order-asc", "desde,desc", "desde,asc", "orden,desc", "orden,asc")


@dataclass(frozen=True)
class PjnSearchPage:
    payload: JsonDict
    hits: list[JsonDict]
    total: int | None
    size: int | None
    number: int
    fetched_url: str
    headers: JsonDict


@dataclass(frozen=True)
class PjnFacetsPage:
    payload: Any
    facets: JsonDict
    fetched_url: str
    headers: JsonDict


@dataclass(frozen=True)
class PjnAttachmentResponse:
    response: httpx.Response
    fallback_from: JsonDict | None = None


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    _add_query_arguments(parser)
    parser.add_argument("--page", type=_non_negative_int, help="zero-based PJN result page")


def add_facets_arguments(parser: argparse.ArgumentParser) -> None:
    _add_query_arguments(parser)


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("document_id", nargs="?", help="numeric PJN document id")
    parser.add_argument("--id", dest="id", help="numeric PJN document id")
    parser.add_argument("--out", help="optional output path for writing the attachment bytes")


def _add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--terms", "--text", "--q", dest="terms", help="free text terms")
    parser.add_argument("--dependencia", help="PJN dependency facet id")
    parser.add_argument("--rubro", help="PJN rubro facet id")
    parser.add_argument("--subrubro", help="PJN subrubro facet id")
    parser.add_argument("--from", dest="date_from", help="publication date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="publication date upper bound, YYYY-MM-DD")
    parser.add_argument("--number", help="document number filter")
    parser.add_argument("--year", help="document year filter")
    parser.add_argument("--sort", help=f"sort alias or API value: {', '.join(SORT_CHOICES)}")
    parser.add_argument(
        "--allow-broad-search",
        "--allow-empty-search",
        action="store_true",
        help="allow empty portal-wide PJN document API queries",
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor)
    query = _query_from_args(args, cursor_payload=cursor_payload, operation="search")
    if not query["query_string"] and not bool(query.get("allow_broad_search")):
        raise usage_error(
            "empty PJN document API search is portal-wide; pass --allow-broad-search to run it explicitly",
            details={"source": SOURCE_ID, "operation": "search"},
        )

    limit = int(args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    requested_page = _requested_page(args, cursor_payload)
    offset = _cursor_offset(cursor_payload)
    warnings = _scope_warnings(query)

    with _make_client() as client:
        search_page = fetch_search_page(
            query_string=query["query_string"],
            sort=_optional_text(query.get("sort")),
            page=requested_page,
            client=client,
        )

    page_offset = min(offset, len(search_page.hits))
    selected_hits = search_page.hits[page_offset : page_offset + limit]
    items = [
        hit_to_item(hit, search_page=search_page, include_raw=bool(args.raw))
        for hit in selected_hits
    ]
    next_cursor = _next_cursor(
        search_page=search_page,
        query=query,
        limit=limit,
        offset=page_offset,
        returned_count=len(items),
    )
    has_more = next_cursor is not None
    page_size = search_page.size or len(search_page.hits)
    global_offset = (search_page.number * page_size) + page_offset

    response_query = _response_query(query, page=search_page.number, limit=limit, offset=global_offset)
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query=response_query,
        items=items,
        page=PageInfo(
            limit=limit,
            offset=global_offset,
            page=search_page.number,
            total=search_page.total,
            has_more=has_more,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=_source_response_id("search", query, search_page.number),
            raw={
                "headers": search_page.headers,
                "spring_page": _spring_page_metadata(search_page.payload),
                "backend_page_size": page_size,
                "backend_offset": page_offset,
            },
        ),
        warnings=warnings,
    )


def handle_facets(args: argparse.Namespace) -> LegalResponse:
    query = _query_from_args(args, cursor_payload={}, operation="facets")
    warnings = _scope_warnings(query)
    with _make_client() as client:
        facets_page = fetch_facets_page(
            query_string=query["query_string"],
            sort=_optional_text(query.get("sort")),
            client=client,
        )

    raw: JsonDict = {"headers": facets_page.headers, "facet_count": _facet_count(facets_page.payload)}
    if bool(args.raw):
        raw["payload"] = facets_page.payload
    return LegalResponse(
        ok=True,
        source=SOURCE_ID,
        operation="facets",
        query=_response_query(query),
        facets=facets_page.facets,
        provenance=_provenance(
            fetched_urls=[facets_page.fetched_url],
            source_response_id=_source_response_id("facets", query, None),
            raw=raw,
        ),
        warnings=warnings,
    )


def handle_download(args: argparse.Namespace) -> LegalResponse:
    document_id = _download_document_id(args)
    url = ATTACHMENT_URL_TEMPLATE.format(document_id=document_id)
    output_path = _optional_text(getattr(args, "out", None))

    with _make_client() as client:
        if output_path:
            attachment = fetch_attachment_bytes(url, client=client)
            output = write_attachment(output_path, attachment.response.content)
        else:
            attachment = fetch_attachment_metadata(url, client=client)
            output = None

    document = attachment_response_to_document(
        document_id=document_id,
        url=url,
        attachment=attachment,
        output=output,
        include_raw=bool(args.raw),
    )
    request: JsonDict = {"id": document_id}
    if output is not None:
        request["out"] = output["path"]
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request=request,
        document=document,
        provenance=document.provenance,
    )


def fetch_search_page(
    *,
    query_string: str,
    sort: str | None = DEFAULT_SORT,
    page: int = DEFAULT_PAGE,
    client: LegalHttpClient | None = None,
) -> PjnSearchPage:
    owns_client = client is None
    http = client or _make_client()
    params: JsonDict = {"query": query_string, "page": page}
    if sort:
        params["sort"] = sort
    try:
        response = http.request("GET", SEARCH_URL, params=params)
        return parse_search_response(response)
    finally:
        if owns_client:
            http.close()


def fetch_facets_page(
    *,
    query_string: str,
    sort: str | None = DEFAULT_SORT,
    client: LegalHttpClient | None = None,
) -> PjnFacetsPage:
    owns_client = client is None
    http = client or _make_client()
    params: JsonDict = {"query": query_string}
    if sort:
        params["sort"] = sort
    try:
        response = http.request("GET", FACETS_URL, params=params)
        return parse_facets_response(response)
    finally:
        if owns_client:
            http.close()


def fetch_attachment_metadata(url: str, *, client: LegalHttpClient | None = None) -> PjnAttachmentResponse:
    owns_client = client is None
    http = client or _make_client()
    try:
        try:
            return PjnAttachmentResponse(response=http.head(url))
        except LegalCliError as exc:
            if _status_code(exc) not in {403, 405, 501}:
                raise
            response = http.request("GET", url, headers={"Range": "bytes=0-0"})
            return PjnAttachmentResponse(response=response, fallback_from=_error_evidence(exc))
    finally:
        if owns_client:
            http.close()


def fetch_attachment_bytes(url: str, *, client: LegalHttpClient | None = None) -> PjnAttachmentResponse:
    owns_client = client is None
    http = client or _make_client()
    try:
        return PjnAttachmentResponse(response=http.request("GET", url))
    finally:
        if owns_client:
            http.close()


def write_attachment(output_path: str, content: bytes) -> JsonDict:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    byte_count = path.write_bytes(content)
    return {
        "path": str(path),
        "bytes": byte_count,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def parse_search_response(response: httpx.Response) -> PjnSearchPage:
    payload = _json_payload(response, "PJN document search response was not valid JSON")
    if not isinstance(payload, Mapping):
        raise parse_error(
            "PJN document search payload must be a JSON object",
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        raise parse_error(
            "PJN document search payload is missing content",
            details={"payload_keys": list(payload.keys())},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )

    hits: list[JsonDict] = []
    for hit in raw_content:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "PJN document search content entries must be JSON objects",
                details={"entry_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[str(response.url)]),
            )
        hits.append(dict(hit))

    return PjnSearchPage(
        payload=dict(payload),
        hits=hits,
        total=_optional_int(payload.get("totalElements")),
        size=_optional_int(payload.get("size")),
        number=_optional_int(payload.get("number")) or 0,
        fetched_url=str(response.url),
        headers=_useful_headers(response),
    )


def parse_facets_response(response: httpx.Response) -> PjnFacetsPage:
    payload = _json_payload(response, "PJN document facets response was not valid JSON")
    return PjnFacetsPage(
        payload=payload,
        facets=parse_facets_payload(payload),
        fetched_url=str(response.url),
        headers=_useful_headers(response),
    )


def parse_facets_payload(payload: Any) -> JsonDict:
    entries = _facet_entries(payload)
    by_type: JsonDict = {}
    for entry in entries:
        facet_type = _facet_type(entry)
        key = _facet_key(facet_type)
        by_type.setdefault(key, []).append(_normalized_facet(entry, facet_type=facet_type))

    facets: JsonDict = {"by_type": by_type, "raw_count": len(entries)}
    if "dependencia" in by_type:
        facets["dependencies"] = by_type["dependencia"]
    if "rubro" in by_type:
        facets["rubros"] = by_type["rubro"]
    if "subrubro" in by_type:
        facets["subrubros"] = by_type["subrubro"]
    return facets


def attachment_response_to_document(
    *,
    document_id: str,
    url: str,
    attachment: PjnAttachmentResponse,
    output: Mapping[str, Any] | None = None,
    include_raw: bool = False,
) -> LegalDocument:
    response = attachment.response
    content_type = _optional_text(response.headers.get("content-type"))
    content_length = _content_length(response.headers)
    content_disposition = _optional_text(response.headers.get("content-disposition"))
    disposition_type, disposition_filename = _content_disposition_parts(content_disposition)
    filename = disposition_filename or _filename_from_url(url)
    kind = _attachment_kind(url=url, content_type=content_type, filename=filename)
    document_key = f"{SOURCE_ID}:download:{document_id}"
    headers = _useful_headers(response)
    output_fields = _compact(
        {
            "path": output.get("path") if output is not None else None,
            "bytes": output.get("bytes") if output is not None else None,
            "sha256": output.get("sha256") if output is not None else None,
        }
    )
    file_entry = _compact(
        {
            "url": url,
            "label": filename or f"pjn-documento-{document_id}",
            "kind": kind,
            "content_type": content_type,
            "content_length": content_length,
            "content_disposition": content_disposition,
            "disposition_type": disposition_type,
            "output": output_fields,
        }
    )
    metadata = _compact(
        {
            "document_id": document_id,
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
            "location": _optional_text(response.headers.get("location")),
            "accept_ranges": _optional_text(response.headers.get("accept-ranges")),
            "method": response.request.method,
            "status_code": response.status_code,
            "output_path": output_fields.get("path"),
            "output_bytes": output_fields.get("bytes"),
            "output_sha256": output_fields.get("sha256"),
        }
    )
    provenance_raw = _compact(
        {
            "method": response.request.method,
            "status_code": response.status_code,
            "headers": headers,
            "fallback_from": attachment.fallback_from,
            "output": output_fields,
        }
    )
    return LegalDocument(
        id=document_key,
        title=filename or f"PJN document attachment {document_id}",
        document_type=kind,
        url=url,
        file_url=url,
        content_type=content_type,
        metadata=metadata,
        links=[{"url": url, "label": filename or f"adjunto {document_id}", "kind": kind}],
        files=[file_entry],
        source_fields=_compact(
            {
                "document_id": document_id,
                "attachment_endpoint": url,
                "output": output_fields,
            }
        ),
        raw={"headers": headers} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=document_key,
            raw=provenance_raw,
        ),
    )


def hit_to_item(
    hit: Mapping[str, Any],
    *,
    search_page: PjnSearchPage,
    include_raw: bool = False,
) -> LegalItem:
    document_id = _document_id(hit)
    file_url = ATTACHMENT_URL_TEMPLATE.format(document_id=document_id)
    title = _first_text(hit, "descripcion", "observacion")
    keywords = _keywords(hit.get("palabrasClaves"))
    document_type = _first_text(hit, "tipoDesc", "tipo", "tipoAdjunto")
    date_value = _hit_date(hit)
    dependencia = _optional_text(hit.get("dependencia"))
    rubro = _optional_text(hit.get("rubro"))
    tipo_adjunto = _optional_text(hit.get("tipoAdjunto"))

    source_fields = _compact(
        {
            "document_id": document_id,
            "status": hit.get("status"),
            "orden": hit.get("orden"),
            "numero": _optional_text(hit.get("numero")),
            "anio": _optional_text(hit.get("anio")),
            "descripcion": title,
            "observacion": _optional_text(hit.get("observacion")),
            "fecha": _optional_text(hit.get("fecha")),
            "desde": _optional_text(hit.get("desde")),
            "hasta": _optional_text(hit.get("hasta")),
            "creado": _optional_text(hit.get("creado")),
            "publicacion": _optional_text(hit.get("publicacion")),
            "visibilidad": _optional_text(hit.get("visibilidad")),
            "tipo_adjunto": tipo_adjunto,
            "palabras_claves": keywords,
            "firmantes": _string_list(hit.get("firmantes")),
            "download": {"url": file_url, "content_type": tipo_adjunto},
        }
    )

    return LegalItem(
        id=f"{SOURCE_ID}:{document_id}",
        title=title,
        date=date_value,
        document_type=document_type,
        url=file_url,
        file_url=file_url,
        snippet=_snippet(hit, keywords=keywords),
        facets=_compact(
            {
                "dependencia": dependencia,
                "rubro": rubro,
                "tipo_adjunto": tipo_adjunto,
            }
        ),
        source_fields=source_fields,
        raw=dict(hit) if include_raw else {},
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=document_id,
        ),
    )


def build_query(
    *,
    terms: str | None = None,
    dependencia: str | None = None,
    rubro: str | None = None,
    subrubro: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    number: str | None = None,
    year: str | None = None,
    sort: str | None = None,
    allow_broad_search: bool = False,
) -> JsonDict:
    normalized_terms = _optional_text(terms)
    normalized_dependencia = _numeric_filter(dependencia, field="dependencia")
    normalized_rubro = _numeric_filter(rubro, field="rubro")
    normalized_subrubro = _numeric_filter(subrubro, field="subrubro")
    normalized_number = _numeric_filter(number, field="number")
    normalized_year = _year_filter(year)
    iso_from, api_from = _api_date(date_from, field="from")
    iso_to, api_to = _api_date(date_to, field="to")
    if iso_from and iso_to and date.fromisoformat(iso_from) > date.fromisoformat(iso_to):
        raise usage_error("--from must be less than or equal to --to")
    canonical_sort = _canonical_sort(sort)

    tokens: list[str] = []
    query: JsonDict = {}
    if normalized_terms:
        tokens.append(f"terms:{normalized_terms}")
        query["terms"] = normalized_terms
    if normalized_dependencia:
        tokens.append(f"depend:{normalized_dependencia}")
        query["dependencia"] = normalized_dependencia
    if normalized_rubro:
        tokens.append(f"rubro:{normalized_rubro}")
        query["rubro"] = normalized_rubro
    if normalized_subrubro:
        tokens.append(f"subrubro:{normalized_subrubro}")
        query["subrubro"] = normalized_subrubro
    if iso_from and api_from:
        tokens.append(f"fecha>{api_from}")
        query["from"] = iso_from
    if iso_to and api_to:
        tokens.append(f"fecha<{api_to}")
        query["to"] = iso_to
    if normalized_number:
        tokens.append(f"num:{normalized_number}")
        query["number"] = normalized_number
    if normalized_year:
        tokens.append(f"anio:{normalized_year}")
        query["year"] = normalized_year

    query["query_string"] = ",".join(tokens)
    query["sort"] = canonical_sort
    if allow_broad_search:
        query["allow_broad_search"] = True
    return query


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"source {SOURCE_ID!r} is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("facets", handle_facets, help="return PJN document API facets", add_arguments=add_facets_arguments)
    adapter.register_operation("search", handle_search, help="search PJN document API", add_arguments=add_search_arguments)
    adapter.register_operation("download", handle_download, help="inspect or write a PJN document attachment", add_arguments=add_download_arguments)
    return adapter


def _query_from_args(
    args: argparse.Namespace,
    *,
    cursor_payload: Mapping[str, Any],
    operation: str,
) -> JsonDict:
    if operation == "search" and cursor_payload and not _has_explicit_query_args(args):
        query = _query_from_cursor(cursor_payload)
        if query is not None:
            return query
    return build_query(
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
    if not isinstance(query, Mapping):
        return None
    if "query_string" not in query:
        return None
    return dict(query)


def _has_explicit_query_args(args: argparse.Namespace) -> bool:
    return any(
        _optional_text(getattr(args, name, None))
        for name in ("terms", "dependencia", "rubro", "subrubro", "date_from", "date_to", "number", "year", "sort")
    ) or bool(getattr(args, "allow_broad_search", False))


def _download_document_id(args: argparse.Namespace) -> str:
    raw = _optional_text(getattr(args, "id", None)) or _optional_text(getattr(args, "document_id", None))
    if not raw:
        raise usage_error("download requires --id", details={"source": SOURCE_ID, "operation": "download"})
    if not raw.isdigit():
        raise usage_error("--id must contain only digits", details={"id": raw})
    return raw


def _decode_cursor(cursor: str | None) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation="search")
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _requested_page(args: argparse.Namespace, cursor_payload: Mapping[str, Any]) -> int:
    page_arg = getattr(args, "page", None)
    if page_arg is not None:
        return int(page_arg)
    page = cursor_payload.get("page")
    if isinstance(page, int) and page >= 0:
        return page
    return DEFAULT_PAGE


def _cursor_offset(cursor_payload: Mapping[str, Any]) -> int:
    offset = cursor_payload.get("offset")
    if isinstance(offset, int) and offset >= 0:
        return offset
    return 0


def _next_cursor(
    *,
    search_page: PjnSearchPage,
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
    elif _has_next_backend_page(search_page):
        next_page = search_page.number + 1
        next_offset = 0
    else:
        return None
    return make_cursor(
        source=SOURCE_ID,
        operation="search",
        page=next_page,
        offset=next_offset,
        limit=limit,
        raw={"query": dict(query)},
    )


def _has_next_backend_page(search_page: PjnSearchPage) -> bool:
    last = search_page.payload.get("last")
    if isinstance(last, bool):
        return not last
    total_pages = _optional_int(search_page.payload.get("totalPages"))
    if total_pages is not None:
        return search_page.number + 1 < total_pages
    if search_page.total is not None and search_page.size:
        return (search_page.number + 1) * search_page.size < search_page.total
    return False


def _response_query(
    query: Mapping[str, Any],
    *,
    page: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> JsonDict:
    response = {key: value for key, value in query.items() if value is not None}
    if page is not None:
        response["page"] = page
    if limit is not None:
        response["limit"] = limit
    if offset is not None:
        response["offset"] = offset
    return response


def _scope_warnings(query: Mapping[str, Any]) -> list[str]:
    return [BROAD_SEARCH_WARNING] if not _optional_text(query.get("query_string")) else []


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"status_code": response.status_code}),
        ) from exc


def _facet_entries(payload: Any) -> list[JsonDict]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        entries: list[JsonDict] = []
        for key, value in payload.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, Mapping):
                        entry = dict(item)
                        entry.setdefault("tipo", key)
                        entries.append(entry)
        return entries
    raise parse_error(
        "PJN document facets payload must be a JSON array or object of arrays",
        details={"payload_type": type(payload).__name__},
    )


def _normalized_facet(entry: Mapping[str, Any], *, facet_type: str | None) -> JsonDict:
    label = _first_text(entry, "titulo", "label", "nombre", "descripcion")
    value = _optional_text(entry.get("id")) or label
    return _compact(
        {
            "value": value,
            "id": entry.get("id"),
            "label": label,
            "count": _optional_int(entry.get("total") or entry.get("count")),
            "type": facet_type,
            "sort": _optional_text(entry.get("ordenamiento") or entry.get("sort")),
        }
    )


def _facet_type(entry: Mapping[str, Any]) -> str | None:
    return _optional_text(entry.get("tipo") or entry.get("type") or entry.get("facet"))


def _facet_key(facet_type: str | None) -> str:
    text = (facet_type or "unknown").casefold().replace(" ", "_")
    return text.replace("-", "_")


def _facet_count(payload: Any) -> int:
    try:
        return len(_facet_entries(payload))
    except Exception:
        return 0


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


def _document_id(hit: Mapping[str, Any]) -> str:
    value = _optional_text(hit.get("id"))
    if value:
        return value
    raise parse_error("PJN document search hit is missing id")


def _hit_date(hit: Mapping[str, Any]) -> str | None:
    for key in ("fecha", "publicacion", "desde", "creado"):
        normalized = normalize_date(_optional_text(hit.get(key)))
        if normalized:
            return normalized
    return None


def _snippet(hit: Mapping[str, Any], *, keywords: list[str]) -> str | None:
    parts: list[str] = []
    if keywords:
        parts.append(", ".join(keywords))
    observacion = _optional_text(hit.get("observacion"))
    if observacion:
        parts.append(observacion)
    return clean_snippet(" | ".join(parts), max_length=SNIPPET_LENGTH) if parts else None


def _keywords(value: Any) -> list[str]:
    return _string_list(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _optional_text(item))]
    text = _optional_text(value)
    return [text] if text else []


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _optional_text(mapping.get(key))
        if text:
            return text
    return None


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


def _numeric_filter(value: str | None, *, field: str) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    if not text.isdigit():
        raise usage_error(f"--{field.replace('_', '-')} must contain only digits")
    return text


def _year_filter(value: str | None) -> str | None:
    text = _numeric_filter(value, field="year")
    if text is None:
        return None
    if len(text) != 4:
        raise usage_error("--year must be a four-digit year")
    return text


def _api_date(value: str | None, *, field: str) -> tuple[str | None, str | None]:
    text = _optional_text(value)
    if not text:
        return None, None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise usage_error(f"--{field} must be an ISO date YYYY-MM-DD") from exc
    return parsed.isoformat(), parsed.strftime("%d%m%Y")


def _canonical_sort(value: str | None) -> str:
    text = _optional_text(value)
    if not text:
        return DEFAULT_SORT
    key = text.casefold().replace("_", "-")
    sort = SORT_ALIASES.get(key)
    if sort is None:
        raise usage_error(
            "--sort must be one of recent, oldest, order-desc, order-asc, desde,desc, desde,asc, orden,desc, orden,asc"
        )
    return sort


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _source_response_id(operation: str, query: Mapping[str, Any], page: int | None) -> str:
    page_part = "" if page is None else f":{page}"
    query_string = _optional_text(query.get("query_string")) or "empty"
    return f"{operation}{page_part}:{query_string[:120]}"


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


def _filename_from_url(url: str | None) -> str | None:
    text = _optional_text(url)
    if not text:
        return None
    path = urlparse(text).path
    name = unquote(path.rsplit("/", 1)[-1])
    return _optional_text(name)


def _extension(filename: str | None) -> str | None:
    text = _optional_text(filename)
    if not text or "." not in text:
        return None
    return text.rsplit(".", 1)[-1].lower()


def _attachment_kind(*, url: str, content_type: str | None, filename: str | None) -> str:
    kind = classify_link(filename or url, base_url=API_BASE_URL, content_type=content_type)
    return "file" if kind in {"page", "relative", "unknown"} else kind


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
