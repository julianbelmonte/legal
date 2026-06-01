"""Jusbaires Juristeca direct search, descriptors, and document adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from legal.errors import LegalCliError, not_found, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import decode_cursor, make_cursor
from legal.parsing import (
    HtmlNode,
    absolute_url,
    classify_link,
    clean_snippet,
    clean_text,
    extract_links,
    normalize_date,
    parse_html,
    text_content,
)
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "jusbaires"
SOURCE_MAP = "legal/docs/jusbaires_jurisprudencia.md"

BASE_URL = "https://juristeca.jusbaires.gob.ar"
HUMAN_URL = f"{BASE_URL}/"
SEARCH_URL = f"{BASE_URL}/buscador-juristeca/busqueda-avanzada-de-jurisprudencia/"
DESCRIPTORS_URL = f"{BASE_URL}/baj/"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 600

FALLO_ID_RE = re.compile(r"fallo-(?P<id>\d+)$", re.IGNORECASE)
SUMARIO_ID_RE = re.compile(r"sumario-(?P<id>\d+)$", re.IGNORECASE)
TOTAL_RE = re.compile(r"(?P<label>Fallos|Sumarios)\s+encontrados\s*\((?P<total>[\d.]+)\)", re.IGNORECASE)
PAGE_RE = re.compile(r"P[aá]gina\s+(?P<page>\d+)", re.IGNORECASE)
DATE_RE = re.compile(r"Fecha\s*:\s*(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", re.IGNORECASE)
SALA_RE = re.compile(r"Sala\s*:\s*(?P<sala>[^.]+)", re.IGNORECASE)
CAUSE_RE = re.compile(
    r"Causa\s+N(?:ro|[º°])?\.?\s*:?\s*(?P<value>.*?)(?=\s+(?:Fecha|Autos|Sala)\s*:|\.|$)",
    re.IGNORECASE,
)
ID_LABEL_RE = re.compile(r"ID\s+(?P<label>Fallo|Sumario)\s*:\s*(?P<id>\d+)", re.IGNORECASE)
FALLO_PDF_ID_RE = re.compile(r"/fallos/(?P<id>\d+)\.pdf$", re.IGNORECASE)


@dataclass(frozen=True)
class SearchPage:
    url: str
    kind: str
    html: str
    items: list[LegalItem]
    total: int | None
    page: int
    next_params: list[tuple[str, str]]
    headers: JsonDict
    no_results: bool = False


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", choices=("fallos", "sumarios"), default="fallos")
    parser.add_argument("--text", "--q", dest="text", action="append", help="required text term or phrase")
    parser.add_argument("--or-text", "--text-or", dest="text_or", action="append", help="optional OR text term")
    parser.add_argument("--not-text", "--text-not", dest="text_not", action="append", help="excluded text term")
    parser.add_argument("--fuero", action="append", help="Fuero[] id; may be repeated or comma-separated")
    parser.add_argument("--sala", action="append", help="Sala[] id; may be repeated or comma-separated")
    parser.add_argument("--from", "--from-date", dest="date_from", help="FechaFalloDesde, YYYY-MM-DD")
    parser.add_argument("--to", "--to-date", dest="date_to", help="FechaFalloHasta, YYYY-MM-DD")
    parser.add_argument("--actor", help="actor text")
    parser.add_argument("--demandado", help="demandado text")
    parser.add_argument("--causa-number", "--numero-causa", dest="causa_number", help="NumeroCausa text")
    parser.add_argument("--descriptor", dest="descriptors", action="append", help="DescriptoresAND[] id/value")
    parser.add_argument("--descriptor-or", dest="descriptors_or", action="append", help="DescriptoresOR[] id/value")
    parser.add_argument("--descriptor-not", dest="descriptors_not", action="append", help="DescriptoresNOT[] id/value")
    parser.add_argument(
        "--descriptor-word",
        dest="descriptor_words",
        action="append",
        help="descriptorPalabraIncluir[] term",
    )
    parser.add_argument(
        "--descriptor-exclude",
        dest="descriptor_excludes",
        action="append",
        help="descriptorPalabraExcluir[] term",
    )


def add_descriptors_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--q", "--text", dest="q", help="descriptor autocomplete query")


def add_fallo_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("fallo_id", nargs="?", help="Jusbaires fallo id")
    parser.add_argument("--id", dest="id_option", help="Jusbaires fallo id")


def add_sumario_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("sumario_id", nargs="?", help="Jusbaires sumario id")
    parser.add_argument("--id", dest="id_option", help="Jusbaires sumario id")


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None), operation="search")
    if cursor_payload:
        raw = cursor_payload.get("raw") if isinstance(cursor_payload.get("raw"), Mapping) else {}
        query = dict(raw.get("query") or {})
        params = _param_pairs(raw.get("params"))
        offset = int(cursor_payload.get("offset") or 0)
    else:
        query = query_from_args(args)
        params = search_params(query)
        offset = 0

    if not params:
        params = search_params(query)
    kind = _kind(query.get("kind"))
    limit = _resolve_limit(args, cursor_payload=cursor_payload)

    with _make_client() as client:
        response = client.request("GET", SEARCH_URL, params=params)
        page = parse_search_response(response, kind=kind, query=query, include_raw=bool(args.raw))

    page_items = page.items
    items = page_items[offset : offset + limit]
    next_cursor = _next_cursor(
        query=query,
        params=params,
        page=page,
        offset=offset,
        limit=limit,
        returned_count=len(items),
    )
    total = page.total if page.total is not None else len(page_items)
    warnings = _warnings_for_search_page(page)

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "limit": limit, "offset": offset},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=offset,
            page=page.page,
            total=total,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            fetched_urls=[page.url],
            source_response_id=f"{kind}:{page.page}",
            raw={
                "headers": page.headers,
                "params": _params_to_dict(params),
                "result_count": len(page.items),
                "next_params": _params_to_dict(page.next_params) if page.next_params else {},
                "no_results": page.no_results,
            },
        ),
        facets=_facets_for_search(),
        warnings=warnings,
    )


def handle_descriptors(args: argparse.Namespace) -> LegalResponse:
    q = _required_text(getattr(args, "q", None), field="q")
    limit = int(args.limit or DEFAULT_LIMIT)

    with _make_client() as client:
        response = client.request("GET", DESCRIPTORS_URL, params={"accion": "descriptores", "q": q})
        payload = _json_payload(response)

    items = descriptors_to_items(payload, fetched_url=str(response.url), limit=limit, include_raw=bool(args.raw))
    total = _descriptor_total(payload)
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="descriptors",
        query={"q": q, "limit": limit},
        items=items,
        page=PageInfo(limit=limit, offset=0, page=1, total=total, has_more=total is not None and len(items) < total),
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=f"descriptors:{q}",
            raw={"headers": _useful_headers(response), "option_count": total},
        ),
    )


def handle_fallo(args: argparse.Namespace) -> LegalResponse:
    fallo_id = _required_id(getattr(args, "id_option", None) or getattr(args, "fallo_id", None), field="id")
    pdf_url = fallo_pdf_url(fallo_id)

    with _make_client() as client:
        response = fetch_pdf_metadata(pdf_url, client=client)

    document = pdf_response_to_document(fallo_id, response=response, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="fallo",
        request={"id": fallo_id, "url": pdf_url},
        document=document,
        provenance=document.provenance,
    )


def handle_sumario(args: argparse.Namespace) -> LegalResponse:
    sumario_id = _required_id(getattr(args, "id_option", None) or getattr(args, "sumario_id", None), field="id")
    query = {"kind": "sumarios", "sumario_ids": [sumario_id]}
    params = [("accion", "sumarios"), ("sumario[]", sumario_id)]

    with _make_client() as client:
        response = client.request("GET", SEARCH_URL, params=params)
        page = parse_search_response(response, kind="sumarios", query=query, include_raw=bool(args.raw))

    item = next((candidate for candidate in page.items if candidate.id == sumario_id), page.items[0] if page.items else None)
    if item is None:
        raise not_found(
            "Jusbaires sumario was not found",
            details={"id": sumario_id},
            provenance=_provenance(
                fetched_urls=[page.url],
                source_response_id=f"sumario:{sumario_id}",
                raw={"headers": page.headers, "params": _params_to_dict(params), "no_results": page.no_results},
            ),
        )

    document = sumario_item_to_document(item, fetched_url=page.url, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="sumario",
        request={"id": sumario_id, "params": _params_to_dict(params)},
        document=document,
        provenance=document.provenance,
    )


def query_from_args(args: argparse.Namespace) -> JsonDict:
    return _compact(
        {
            "kind": _kind(args.kind),
            "text": _texts(getattr(args, "text", None)),
            "text_or": _texts(getattr(args, "text_or", None)),
            "text_not": _texts(getattr(args, "text_not", None)),
            "fuero": _split_values(getattr(args, "fuero", None)),
            "sala": _split_values(getattr(args, "sala", None)),
            "date_from": _date_arg(getattr(args, "date_from", None), field="from"),
            "date_to": _date_arg(getattr(args, "date_to", None), field="to"),
            "actor": _optional_text(getattr(args, "actor", None)),
            "demandado": _optional_text(getattr(args, "demandado", None)),
            "causa_number": _optional_text(getattr(args, "causa_number", None)),
            "descriptors": _texts(getattr(args, "descriptors", None)),
            "descriptors_or": _texts(getattr(args, "descriptors_or", None)),
            "descriptors_not": _texts(getattr(args, "descriptors_not", None)),
            "descriptor_words": _texts(getattr(args, "descriptor_words", None)),
            "descriptor_excludes": _texts(getattr(args, "descriptor_excludes", None)),
        }
    )


def search_params(query: Mapping[str, Any]) -> list[tuple[str, str]]:
    kind = _kind(query.get("kind"))
    params: list[tuple[str, str]] = [("accion", kind)]
    params.append(("buscar-fallo" if kind == "fallos" else "buscar-sumario", "on"))
    _extend_params(params, "cuerpo[]", query.get("text"))
    _extend_params(params, "cuerpoOR[]", query.get("text_or"))
    _extend_params(params, "cuerpoNOT[]", query.get("text_not"))
    _extend_params(params, "Fuero[]", query.get("fuero"))
    _extend_params(params, "Sala[]", query.get("sala"))
    _add_param(params, "FechaFalloDesde", query.get("date_from"))
    _add_param(params, "FechaFalloHasta", query.get("date_to"))
    _add_param(params, "Actor", query.get("actor"))
    _add_param(params, "Demandado", query.get("demandado"))
    _add_param(params, "NumeroCausa", query.get("causa_number"))
    _extend_params(params, "DescriptoresAND[]", query.get("descriptors"))
    _extend_params(params, "DescriptoresOR[]", query.get("descriptors_or"))
    _extend_params(params, "DescriptoresNOT[]", query.get("descriptors_not"))
    _extend_params(params, "descriptorPalabraIncluir[]", query.get("descriptor_words"))
    _extend_params(params, "descriptorPalabraExcluir[]", query.get("descriptor_excludes"))
    return params


def parse_search_response(
    response: httpx.Response,
    *,
    kind: str,
    query: Mapping[str, Any],
    include_raw: bool = False,
) -> SearchPage:
    html = response.text
    fetched_url = str(response.url)
    root = parse_html(html)
    kind = _kind(kind)
    items = parse_fallo_items(root, page_url=fetched_url, include_raw=include_raw) if kind == "fallos" else parse_sumario_items(
        root,
        page_url=fetched_url,
        include_raw=include_raw,
    )
    no_results = "No se obtuvieron resultados para esta búsqueda" in html
    total = _parse_total(root, kind=kind)
    if total is None:
        total = 0 if no_results else len(items)
    return SearchPage(
        url=fetched_url,
        kind=kind,
        html=html,
        items=items,
        total=total,
        page=_parse_page(root),
        next_params=_parse_next_params(root),
        headers=_useful_headers(response),
        no_results=no_results,
    )


def parse_fallo_items(root: HtmlNode, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    items: list[LegalItem] = []
    seen: set[str] = set()
    for node in _iter_by_class(root, "fallo-individual", tag="div"):
        item = fallo_node_to_item(node, page_url=page_url, include_raw=include_raw)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def fallo_node_to_item(node: HtmlNode, *, page_url: str, include_raw: bool = False) -> LegalItem | None:
    fallo_id = _fallo_id(node)
    if not fallo_id:
        return None

    fuero = _labelled_value(node, "Fuero")
    court = _court_text(node)
    caratula = _node_text_by_class(node, "caratula")
    parties = _parse_parties(caratula)
    descriptors = _descriptor_links(node, page_url=page_url)
    links = _dedupe_links(extract_links(node, base_url=page_url))
    pdf_url = _first_pdf_url(links) or fallo_pdf_url(fallo_id)
    sumarios_link = _first_link_with_action(links, "sumarios-del-fallo")
    source_fields = _compact(
        {
            "fallo_id": fallo_id,
            "fuero": fuero,
            "tribunal": court,
            "caratula": parties.get("title"),
            "actor": parties.get("actor"),
            "demandado": parties.get("demandado"),
            "causa_number": parties.get("cause_number"),
            "sala": parties.get("sala"),
            "descriptors": descriptors,
            "sumarios_link": sumarios_link,
            "pdf_url": pdf_url,
        }
    )
    title = parties.get("title") or f"Fallo {fallo_id}"
    return LegalItem(
        id=fallo_id,
        title=title,
        date=parties.get("date"),
        document_type="fallo",
        url=page_url,
        file_url=pdf_url,
        snippet=clean_snippet(caratula or title, max_length=SNIPPET_LENGTH),
        facets=_compact(
            {
                "fuero": fuero,
                "sala": parties.get("sala"),
                "descriptors": [item["id"] for item in descriptors if item.get("id")],
            }
        ),
        source_fields=source_fields,
        raw={"text": node.text(), "attrs": dict(node.attrs)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=f"fallo:{fallo_id}",
            raw={"pdf_url": pdf_url},
        ),
    )


def parse_sumario_items(root: HtmlNode, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    items: list[LegalItem] = []
    seen: set[str] = set()
    for node in _iter_by_class(root, "sumario-individual", tag="div"):
        item = sumario_node_to_item(node, page_url=page_url, include_raw=include_raw)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def sumario_node_to_item(node: HtmlNode, *, page_url: str, include_raw: bool = False) -> LegalItem | None:
    sumario_id = _sumario_id(node)
    if not sumario_id:
        return None

    fallo_id = _id_from_label(node, "Fallo")
    fuero = _labelled_value(node, "Fuero")
    caratula = _node_text_by_class(node, "caratula")
    parties = _parse_parties(caratula)
    descriptors = _descriptor_links(node, page_url=page_url)
    links = _dedupe_links(extract_links(node, base_url=page_url))
    pdf_url = _first_pdf_url(links) or (fallo_pdf_url(fallo_id) if fallo_id else None)
    summary_short = _node_text_by_class(node, "contenido-corto")
    summary_full = _node_text_by_class(node, "contenido-completo") or summary_short
    title = parties.get("title") or f"Sumario {sumario_id}"
    source_fields = _compact(
        {
            "sumario_id": sumario_id,
            "fallo_id": fallo_id,
            "fuero": fuero,
            "caratula": title,
            "actor": parties.get("actor"),
            "demandado": parties.get("demandado"),
            "causa_number": parties.get("cause_number"),
            "sala": parties.get("sala"),
            "votantes": _node_text_by_class(node, "votantes"),
            "adherentes": _node_text_by_class(node, "adherentes"),
            "summary_short": summary_short,
            "summary_full": summary_full,
            "descriptors": descriptors,
            "pdf_url": pdf_url,
        }
    )
    return LegalItem(
        id=sumario_id,
        title=title,
        date=parties.get("date"),
        document_type="sumario",
        url=sumario_url(sumario_id),
        file_url=pdf_url,
        snippet=clean_snippet(summary_short or summary_full or caratula, max_length=SNIPPET_LENGTH),
        facets=_compact(
            {
                "fuero": fuero,
                "sala": parties.get("sala"),
                "descriptors": [item["id"] for item in descriptors if item.get("id")],
            }
        ),
        source_fields=source_fields,
        raw={"text": node.text(), "attrs": dict(node.attrs)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=f"sumario:{sumario_id}",
            raw={"pdf_url": pdf_url, "fallo_id": fallo_id},
        ),
    )


def descriptors_to_items(
    payload: Any,
    *,
    fetched_url: str,
    limit: int,
    include_raw: bool = False,
) -> list[LegalItem]:
    options = _descriptor_options(payload)
    items: list[LegalItem] = []
    for option in options[:limit]:
        value = _required_text(option.get("value"), field="descriptor value")
        label = _required_text(option.get("text"), field="descriptor text")
        ids = [part for part in (clean_text(item) for item in value.split(",")) if part]
        items.append(
            LegalItem(
                id=value,
                title=label,
                document_type="descriptor",
                facets={"ids": ids},
                source_fields={"value": value, "ids": ids, "text": label},
                raw=dict(option) if include_raw else {},
                provenance=_provenance(
                    fetched_urls=[fetched_url],
                    source_response_id=f"descriptor:{value}",
                    raw={"value": value},
                ),
            )
        )
    return items


def fetch_pdf_metadata(url: str, *, client: LegalHttpClient | None = None) -> httpx.Response:
    owns_client = client is None
    http = client or _make_client()
    try:
        try:
            return http.head(url)
        except LegalCliError as exc:
            if _status_code(exc) not in {405, 501}:
                raise
            return http.request("GET", url, headers={"Range": "bytes=0-0"})
    finally:
        if owns_client:
            http.close()


def pdf_response_to_document(fallo_id: str, *, response: httpx.Response, include_raw: bool = False) -> LegalDocument:
    url = fallo_pdf_url(fallo_id)
    content_type = _optional_text(response.headers.get("content-type"))
    content_length = _content_length(response.headers)
    filename = f"{fallo_id}.pdf"
    metadata = _compact(
        {
            "fallo_id": fallo_id,
            "filename": filename,
            "extension": ".pdf",
            "kind": classify_link(url, base_url=BASE_URL, content_type=content_type),
            "content_type": content_type,
            "content_length": content_length,
            "last_modified": _optional_text(response.headers.get("last-modified")),
            "etag": _optional_text(response.headers.get("etag")),
            "method": response.request.method,
            "status_code": response.status_code,
        }
    )
    return LegalDocument(
        id=fallo_id,
        title=f"Fallo {fallo_id}",
        document_type="fallo",
        url=url,
        file_url=url,
        content_type=content_type,
        metadata=metadata,
        links=[{"url": url, "label": filename, "kind": "pdf"}],
        files=[{"url": url, "filename": filename, "kind": "pdf", "content_type": content_type, "content_length": content_length}],
        source_fields={"fallo_id": fallo_id, "pdf_url": url},
        raw={"headers": dict(response.headers)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=f"fallo:{fallo_id}",
            raw={"headers": _useful_headers(response), "method": response.request.method, "status_code": response.status_code},
        ),
    )


def sumario_item_to_document(item: LegalItem, *, fetched_url: str, include_raw: bool = False) -> LegalDocument:
    fields = dict(item.source_fields)
    body = _optional_text(fields.get("summary_full")) or _optional_text(fields.get("summary_short"))
    links = _sumario_document_links(item)
    files = [link for link in links if link.get("kind") == "pdf"]
    metadata = _compact(
        {
            "sumario_id": item.id,
            "fallo_id": fields.get("fallo_id"),
            "fuero": fields.get("fuero"),
            "sala": fields.get("sala"),
            "case_number": fields.get("causa_number"),
            "actor": fields.get("actor"),
            "demandado": fields.get("demandado"),
            "descriptors": fields.get("descriptors"),
            "votantes": fields.get("votantes"),
            "adherentes": fields.get("adherentes"),
        }
    )
    return LegalDocument(
        id=item.id,
        title=item.title,
        date=item.date,
        document_type="sumario",
        body=body,
        url=item.url,
        file_url=item.file_url,
        content_type="text/html",
        text_format="plain_text",
        metadata=metadata,
        links=links,
        files=files,
        source_fields=fields,
        raw=dict(item.raw) if include_raw else {},
        provenance=_provenance(
            fetched_urls=[fetched_url],
            source_response_id=f"sumario:{item.id}",
            raw={"fallo_id": fields.get("fallo_id"), "pdf_url": item.file_url},
        ),
    )


def fallo_pdf_url(fallo_id: str) -> str:
    return f"{BASE_URL}/fallos/{fallo_id}.pdf"


def sumario_url(sumario_id: str) -> str:
    return f"{SEARCH_URL}?accion=sumarios&sumario%5B%5D={sumario_id}"


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _resolve_limit(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> int:
    value = getattr(args, "limit", None) or cursor_payload.get("limit") or DEFAULT_LIMIT
    return int(value)


def _next_cursor(
    *,
    query: Mapping[str, Any],
    params: Sequence[tuple[str, str]],
    page: SearchPage,
    offset: int,
    limit: int,
    returned_count: int,
) -> str | None:
    next_offset = offset + returned_count
    if returned_count and next_offset < len(page.items):
        return make_cursor(
            source=SOURCE_ID,
            operation="search",
            page=page.page,
            offset=next_offset,
            limit=limit,
            raw={"query": dict(query), "params": list(params)},
        )
    if page.next_params:
        return make_cursor(
            source=SOURCE_ID,
            operation="search",
            page=page.page + 1,
            offset=0,
            limit=limit,
            raw={"query": dict(query), "params": page.next_params},
        )
    return None


def _warnings_for_search_page(page: SearchPage) -> list[str]:
    if page.no_results and not page.items:
        return ["source returned no result rows for this query"]
    if page.total is not None and page.total > len(page.items) and not page.next_params:
        return ["source reported more results than were present in the parsed page, but no direct continuation form was found"]
    return []


def _json_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            "Jusbaires descriptors response was not valid JSON",
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(
                fetched_urls=[str(response.url)],
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(response.text, max_length=500)},
            ),
        ) from exc


def _descriptor_options(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise parse_error("Jusbaires descriptors payload was not an object", details={"payload_type": type(payload).__name__})
    options = payload.get("options")
    if not isinstance(options, list):
        raise parse_error("Jusbaires descriptors payload did not include an options list")
    return [item for item in options if isinstance(item, Mapping)]


def _descriptor_total(payload: Any) -> int | None:
    try:
        return len(_descriptor_options(payload))
    except LegalCliError:
        return None


def _parse_total(root: HtmlNode, *, kind: str) -> int | None:
    expected = "fallos" if kind == "fallos" else "sumarios"
    for node in root.iter():
        text = node.text()
        if not text:
            continue
        match = TOTAL_RE.search(text)
        if match and match.group("label").lower().startswith(expected[:-1]):
            return _parse_int(match.group("total"))
    return None


def _parse_page(root: HtmlNode) -> int:
    for node in root.iter("h5"):
        text = node.text()
        if not text:
            continue
        match = PAGE_RE.search(text)
        if match:
            return int(match.group("page"))
    return 1


def _parse_next_params(root: HtmlNode) -> list[tuple[str, str]]:
    container = _node_by_id(root, "boton-siguiente")
    if container is None:
        return []
    form = next(container.iter("form"), None)
    if form is None:
        return []
    params: list[tuple[str, str]] = []
    for item in form.iter("input"):
        name = _optional_text(item.get("name"))
        value = _optional_text(item.get("value")) or ""
        if name:
            params.append((name, value))
    return params


def _fallo_id(node: HtmlNode) -> str | None:
    node_id = _optional_text(node.get("id"))
    if node_id:
        match = FALLO_ID_RE.search(node_id)
        if match:
            return match.group("id")
    return _id_from_label(node, "Fallo") or _input_value(node, "fallos_seleccionados[]")


def _sumario_id(node: HtmlNode) -> str | None:
    node_id = _optional_text(node.get("id"))
    if node_id:
        match = SUMARIO_ID_RE.search(node_id)
        if match:
            return match.group("id")
    return _id_from_label(node, "Sumario") or _input_value(node, None, class_name="checkbox-sumario")


def _id_from_label(node: HtmlNode, label: str) -> str | None:
    text = node.text() or ""
    for match in ID_LABEL_RE.finditer(text):
        if match.group("label").lower() == label.lower():
            return match.group("id")
    return None


def _input_value(node: HtmlNode, name: str | None, *, class_name: str | None = None) -> str | None:
    for item in node.iter("input"):
        if name is not None and item.get("name") != name:
            continue
        if class_name is not None and not _has_class(item, class_name):
            continue
        value = _optional_text(item.get("value"))
        if value:
            return value
    return None


def _labelled_value(node: HtmlNode, label: str) -> str | None:
    normalized = _normalize_key(label)
    for paragraph in node.iter("p"):
        text = paragraph.text()
        if not text:
            continue
        if _normalize_key(text).startswith(f"{normalized}:"):
            return clean_text(text.split(":", 1)[1])
    return None


def _court_text(node: HtmlNode) -> str | None:
    for paragraph in _child_nodes(node, {"p"}):
        text = paragraph.text()
        if not text:
            continue
        key = _normalize_key(text)
        if any(marker in key for marker in ("fuero:", "id fallo", "causa", "fecha", "sala")):
            continue
        if _has_class(paragraph, "caratula", "descriptores", "ver"):
            continue
        return text
    return None


def _parse_parties(value: str | None) -> JsonDict:
    text = _optional_text(value)
    if not text:
        return {}
    date = normalize_date(_regex_group(DATE_RE, text, "date"))
    sala = _optional_text(_regex_group(SALA_RE, text, "sala"))
    cause_number = _optional_text(_regex_group(CAUSE_RE, text, "value"))
    title = _title_from_caratula(text)
    actor, demandado = _split_parties(title)
    return _compact(
        {
            "title": title,
            "actor": actor,
            "demandado": demandado,
            "cause_number": cause_number,
            "date": date,
            "sala": sala,
        }
    )


def _title_from_caratula(text: str) -> str | None:
    title = re.split(r"\s+Causa\s+N(?:ro|[º°])?\.?\s*:?", text, maxsplit=1, flags=re.IGNORECASE)[0]
    title = re.split(r"\s+Fecha\s*:", title, maxsplit=1, flags=re.IGNORECASE)[0]
    title = re.split(r"\s+Sala\s*:", title, maxsplit=1, flags=re.IGNORECASE)[0]
    return clean_text(title.lstrip("• ").strip(" ."))


def _split_parties(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    match = re.search(r"\s+c/\s+", title, flags=re.IGNORECASE)
    if not match:
        return None, None
    actor = clean_text(title[: match.start()].strip(" ."))
    demandado = clean_text(title[match.end() :].strip(" ."))
    return actor, demandado


def _descriptor_links(node: HtmlNode, *, page_url: str) -> list[JsonDict]:
    descriptors: list[JsonDict] = []
    for container in _iter_by_class(node, "descriptores"):
        for anchor in container.iter("a"):
            label = _optional_text(anchor.text())
            url = absolute_url(page_url, anchor.get("href"))
            descriptor_id = _descriptor_id_from_url(url)
            if not label and not descriptor_id:
                continue
            descriptors.append(
                _compact(
                    {
                        "id": descriptor_id,
                        "label": label,
                        "url": url,
                        "scope": "fallos" if url and "temas-fallos" in url else "sumarios",
                    }
                )
            )
    return descriptors


def _descriptor_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("DescriptoresAND[]")
    return _optional_text(values[0]) if values else None


def _first_pdf_url(links: Sequence[Mapping[str, Any]]) -> str | None:
    for link in links:
        url = _optional_text(link.get("url"))
        if url and FALLO_PDF_ID_RE.search(urlparse(url).path):
            return url
    return None


def _first_link_with_action(links: Sequence[Mapping[str, Any]], action: str) -> str | None:
    for link in links:
        url = _optional_text(link.get("url"))
        if not url:
            continue
        if parse_qs(urlparse(url).query).get("accion") == [action]:
            return url
    return None


def _sumario_document_links(item: LegalItem) -> list[JsonDict]:
    fields = item.source_fields
    links: list[JsonDict] = []
    if item.url:
        links.append({"url": item.url, "label": "Sumario", "kind": "page"})
    fallo_id = _optional_text(fields.get("fallo_id"))
    if fallo_id:
        links.append({"url": fallo_pdf_url(fallo_id), "label": f"Fallo {fallo_id}", "kind": "pdf"})
    for descriptor in fields.get("descriptors") or []:
        if isinstance(descriptor, Mapping) and descriptor.get("url"):
            links.append({"url": descriptor["url"], "label": descriptor.get("label") or descriptor["url"], "kind": "page"})
    return _dedupe_links(links)


def _node_text_by_class(node: HtmlNode, class_name: str) -> str | None:
    matches = _iter_by_class(node, class_name)
    found = matches[0] if matches else None
    return text_content(found) if found is not None else None


def _node_by_id(root: HtmlNode, node_id: str) -> HtmlNode | None:
    for node in root.iter():
        if node.get("id") == node_id:
            return node
    return None


def _iter_by_class(root: HtmlNode, *class_names: str, tag: str | None = None) -> Sequence[HtmlNode]:
    return [node for node in root.iter(tag) if _has_class(node, *class_names)]


def _has_class(node: HtmlNode, *class_names: str) -> bool:
    classes = set((_optional_text(node.get("class")) or "").split())
    return all(class_name in classes for class_name in class_names)


def _child_nodes(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _regex_group(pattern: re.Pattern[str], text: str, group: str) -> str | None:
    match = pattern.search(text)
    return match.group(group) if match else None


def _kind(value: Any) -> str:
    kind = _optional_text(value) or "fallos"
    if kind not in {"fallos", "sumarios"}:
        raise usage_error("kind must be fallos or sumarios", details={"kind": value})
    return kind


def _date_arg(value: Any, *, field: str) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    parsed = normalize_date(text)
    if not parsed:
        raise usage_error(f"{field} date must be YYYY-MM-DD or a recognized date", details={field: value})
    return parsed


def _texts(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return []
    return [text for text in (_optional_text(value) for value in values) if text]


def _split_values(values: Any) -> list[str]:
    output: list[str] = []
    for value in _texts(values):
        output.extend(part for part in (clean_text(item) for item in value.split(",")) if part)
    return output


def _extend_params(params: list[tuple[str, str]], name: str, values: Any) -> None:
    for value in _texts(values):
        params.append((name, value))


def _add_param(params: list[tuple[str, str]], name: str, value: Any) -> None:
    text = _optional_text(value)
    if text:
        params.append((name, text))


def _param_pairs(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Sequence) or isinstance(item, str | bytes | bytearray) or len(item) != 2:
            continue
        name = _optional_text(item[0])
        if name:
            pairs.append((name, _optional_text(item[1]) or ""))
    return pairs


def _params_to_dict(params: Sequence[tuple[str, str]]) -> JsonDict:
    output: JsonDict = {}
    for key, value in params:
        if key in output:
            existing = output[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                output[key] = [existing, value]
        else:
            output[key] = value
    return output


def _required_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error(f"{field} is required", details={"field": field})
    return text


def _required_id(value: Any, *, field: str) -> str:
    text = _required_text(value, field=field)
    if not re.fullmatch(r"\d+", text):
        raise usage_error(f"{field} must be a numeric Jusbaires id", details={field: value})
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _parse_int(value: Any) -> int | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        return int(text.replace(".", ""))
    except ValueError:
        return None


def _content_length(headers: httpx.Headers) -> int | None:
    content_range = headers.get("content-range")
    if content_range:
        match = re.search(r"/(?P<total>\d+)$", content_range)
        if match:
            return int(match.group("total"))
    return _parse_int(headers.get("content-length"))


def _status_code(error: LegalCliError) -> int | None:
    details = error.details or {}
    status = details.get("status_code")
    return status if isinstance(status, int) else None


def _dedupe_links(links: Sequence[Mapping[str, Any]]) -> list[JsonDict]:
    seen: set[str] = set()
    output: list[JsonDict] = []
    for link in links:
        url = _optional_text(link.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        output.append({str(key): value for key, value in link.items() if value is not None})
    return output


def _normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


def _facets_for_search() -> JsonDict:
    return {
        "kinds": ["fallos", "sumarios"],
        "fuero_ids": ["1", "2", "3"],
        "sala_ids": ["1", "2", "3", "4", "5", "6", "8", "9", "10", "11"],
        "descriptor_operations": ["descriptors", "descriptor", "descriptor-or", "descriptor-not"],
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
    return {key.lower(): value for key, value in response.headers.items() if key.lower() in allowed}


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[HUMAN_URL, SEARCH_URL, DESCRIPTORS_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7"})


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"{SOURCE_ID} source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("search", handle_search, help="search Juristeca fallos or sumarios", add_arguments=add_search_arguments)
    adapter.register_operation("descriptors", handle_descriptors, help="search Juristeca descriptor autocomplete", add_arguments=add_descriptors_arguments)
    adapter.register_operation("fallo", handle_fallo, help="fetch Juristeca fallo PDF metadata", add_arguments=add_fallo_arguments)
    adapter.register_operation("sumario", handle_sumario, help="fetch Juristeca sumario detail", add_arguments=add_sumario_arguments)
    return adapter


register_adapter(build_adapter(), replace=True)
