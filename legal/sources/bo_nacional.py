"""Boletin Oficial Nacional advanced-search adapter."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import json
import os
import re
import socket
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

from apps.legal.cache import SearchCacheRecord, load_search_state, save_search_state
from apps.legal.errors import LegalCliError, not_found, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from apps.legal.parsing import (
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
from apps.legal.registry import get_source
from apps.legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "bo-nacional"
SOURCE_MAP = "apps/legal/docs/boletin_oficial_nacional.md"

BASE_URL = "https://www.boletinoficial.gob.ar"
SEED_URL = f"{BASE_URL}/busquedaAvanzada/all"
SEARCH_URL = f"{BASE_URL}/busquedaAvanzada/realizarBusqueda"
SEARCH_SECOND_URL = f"{SEARCH_URL}/segunda"
RUBROS_URL = f"{BASE_URL}/busquedaAvanzada/{{section}}/rubros"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 500
AJAX_ACCEPT = "application/json, text/javascript, */*; q=0.01"
DEFAULT_RESOLVE_IPS = ("200.108.150.10", "200.108.151.10")
RESOLVE_ENV = "LEGAL_BO_NACIONAL_RESOLVE"

SECTION_IDS: Mapping[str, int] = {"primera": 1, "segunda": 2, "tercera": 3}
SECTION_BY_ID: Mapping[int, str] = {value: key for key, value in SECTION_IDS.items()}
SECTION_ALIASES: Mapping[str, str] = {
    "1": "primera",
    "i": "primera",
    "primera": "primera",
    "primera seccion": "primera",
    "primera sección": "primera",
    "2": "segunda",
    "ii": "segunda",
    "segunda": "segunda",
    "segunda seccion": "segunda",
    "segunda sección": "segunda",
    "3": "tercera",
    "iii": "tercera",
    "tercera": "tercera",
    "tercera seccion": "tercera",
    "tercera sección": "tercera",
    "all": "all",
    "todas": "all",
    "todos": "all",
}
MODE_ALIASES: Mapping[str, str] = {
    "all": "all",
    "todas": "all",
    "todas-las-palabras": "all",
    "todas las palabras": "all",
    "and": "all",
    "any": "any",
    "alguna": "any",
    "algunas": "any",
    "alguna-de-las-palabras": "any",
    "alguna de las palabras": "any",
    "or": "any",
}

_DETAIL_RE = re.compile(
    r"/detalleAviso/(?P<section>primera|segunda|tercera)/(?P<notice_id>\d+)/(?P<date>\d{8})",
    re.IGNORECASE,
)
_PUBLICATION_DATE_RE = re.compile(
    r"Fecha\s+de\s+Publicaci[oó]n\s*:\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
_DETAIL_PUBLICATION_DATE_RE = re.compile(
    r"Fecha\s+de\s+publicaci[oó]n\s*:?\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
_NORM_TYPE_RE = re.compile(r"^(?P<kind>[^\d/]+?)\s+\d", re.IGNORECASE)
_NORM_PARTS_RE = re.compile(
    r"^(?P<kind>[^\d]+?)\s+(?:N[°º]?\s*)?(?P<number>\d[\d./-]*(?:/\d{2,4})?)$",
    re.IGNORECASE,
)
_GDE_CODE_RE = re.compile(r"\b(?P<code>[A-Z]{2,}-\d{4}-\d+[-A-Z0-9#]+)\b")
_PDF_AVISO_RE = re.compile(r"renderPDFAviso\(\s*['\"](?P<path>[^'\"]+)['\"]\s*\)", re.IGNORECASE)
_DOWNLOAD_AVISO_RE = re.compile(r"descargarPDFAviso\((?P<args>[^)]*)\)", re.IGNORECASE)
_JS_QUOTED_ARG_RE = re.compile(r"""["']([^"']*)["']""")


@dataclass(frozen=True)
class SeedPage:
    url: str
    html: str
    headers: JsonDict


@dataclass(frozen=True)
class BoNacionalSearchPage:
    url: str
    items: list[LegalItem]
    total: int | None
    source_page: JsonDict
    headers: JsonDict


@dataclass(frozen=True)
class BoNacionalDetail:
    section: str
    notice_id: str
    notice_date: str
    url: str
    html: str
    headers: JsonDict


class BoNacionalHttpClient(LegalHttpClient):
    """LegalHttpClient with an optional source-local DNS override."""

    def __init__(
        self,
        *args: Any,
        resolve_host: str | None = None,
        resolve_ips: Sequence[str] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._resolve_host = resolve_host
        self._resolve_ips = tuple(resolve_ips)

    def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        with _dns_override(self._resolve_host, self._resolve_ips):
            return super().request(method, url, **kwargs)


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--section", help="section: primera, segunda, tercera, or all")
    parser.add_argument("--keywords", "--text", "--q", dest="keywords", help="advanced-search keywords")
    parser.add_argument("--mode", help="keyword mode: all/todas or any/alguna")
    parser.add_argument("--from", dest="date_from", help="publication date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="publication date upper bound, YYYY-MM-DD")
    parser.add_argument("--number", help="norm number filter")
    parser.add_argument("--year", help="norm year filter")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--section", help="section: primera, segunda, or tercera")
    parser.add_argument("--id", dest="notice_id", help="Boletin Oficial notice id")
    parser.add_argument("--date", help="publication date, YYYY-MM-DD")


def handle_search(args: argparse.Namespace) -> LegalResponse:
    query = _query_from_args(args)
    limit = int(args.limit or DEFAULT_LIMIT)

    with _make_client() as client:
        seed = fetch_seed_page(client=client)
        search_page = fetch_search_page(query=query, client=client, include_raw=bool(args.raw))
        items = search_page.items[:limit]
        total = search_page.total if search_page.total is not None else len(search_page.items)
        has_more = _has_more(
            total=total,
            page_start_offset=0,
            available_count=len(search_page.items),
            returned_count=len(items),
            source_exhausted=not search_page.items,
        )
        search_id = (
            _save_search_state(
                client=client,
                query=query,
                limit=limit,
                form_data=search_form_data(query),
                search_page=search_page,
                returned_items=items,
                fetched_urls=[seed.url, search_page.url],
            )
            if has_more
            else None
        )


    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "limit": limit},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=0,
            page=1,
            total=total,
            has_more=has_more,
            search_id=search_id,
        ),
        provenance=_provenance(
            fetched_urls=[seed.url, search_page.url],
            source_response_id=_source_response_id(query),
            raw={
                "seed_headers": seed.headers,
                "search_headers": search_page.headers,
                "source_page": search_page.source_page,
                "endpoint": _endpoint_for_query(query),
                "dns": _dns_evidence(),
                "search_id": search_id,
            },
        ),
        facets={
            "sections": dict(SECTION_IDS),
            "keyword_modes": {"all": "todas las palabras", "any": "alguna de las palabras"},
        },
    )


def handle_next(args: argparse.Namespace) -> LegalResponse:
    search_id = _required_search_id(args.search_id)
    record = load_search_state(search_id)
    if record is None:
        raise not_found(
            "Boletin Oficial Nacional search state was not found or expired",
            details={"search_id": search_id},
        )
    _validate_search_record(record)
    state = record.cursor_payload
    query = _query_from_record(record)
    limit = int(args.limit or state.get("limit") or DEFAULT_LIMIT)
    cached_items = _cached_page_items(record)
    returned_count = _int_from_state(state, "returned_count") or 0

    if returned_count < len(cached_items):
        return _cached_next_response(
            record=record,
            query=query,
            limit=limit,
            items=cached_items[returned_count : returned_count + limit],
            new_returned_count=min(returned_count + limit, len(cached_items)),
        )

    if bool(state.get("source_exhausted")):
        return _empty_next_response(record=record, query=query, limit=limit)

    source_page = _mapping_from_state(state, "source_page")
    current_page = _int_from_state(state, "current_page") or 1
    page_start_offset = (_int_from_state(state, "page_start_offset") or 0) + len(cached_items)
    form_data = continuation_form_data(
        query,
        source_page=source_page,
        fallback_page=current_page + 1,
    )
    endpoint = _text_from_state(state, "endpoint") or _endpoint_for_query(query)
    referer = _text_from_state(state, "result_url") or SEED_URL

    with _make_client() as client:
        restore_cookies(client=client, cookies=record.cookies)
        search_page = fetch_search_page_form(
            endpoint=endpoint,
            form_data=form_data,
            referer=referer,
            client=client,
            include_raw=bool(args.raw),
        )
        items = search_page.items[:limit]
        effective_total = search_page.total if search_page.total is not None else _int_from_state(state, "total")
        has_more = _has_more(
            total=effective_total,
            page_start_offset=page_start_offset,
            available_count=len(search_page.items),
            returned_count=len(items),
            source_exhausted=not search_page.items,
        )
        _save_search_state(
            client=client,
            query=query,
            limit=limit,
            form_data=form_data,
            search_page=search_page,
            returned_items=items,
            search_id=record.search_id,
            fetched_urls=[referer, search_page.url],
            page_start_offset=page_start_offset,
            current_page=_continuation_page(source_page, fallback=current_page + 1),
            total=effective_total,
        )

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=page_start_offset,
            page=_continuation_page(source_page, fallback=current_page + 1),
            total=effective_total,
            has_more=has_more,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=[referer, search_page.url],
            raw={
                "result_headers": search_page.headers,
                "source_page": search_page.source_page,
                "endpoint": endpoint,
                "search_id": record.search_id,
                "page_start_offset": page_start_offset,
            },
        ),
        facets={
            "sections": dict(SECTION_IDS),
            "keyword_modes": {"all": "todas las palabras", "any": "alguna de las palabras"},
        },
    )


def add_filters_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--section",
        help="restrict rubros to one section: primera, segunda, or tercera (default: all)",
    )


def fetch_rubros(*, section: str, client: LegalHttpClient | None = None) -> list[JsonDict]:
    """Fetch the rubro (category) catalogue for a single Boletin Oficial section."""
    owns_client = client is None
    http = client or _make_client()
    url = RUBROS_URL.format(section=section)
    try:
        try:
            response = http.request(
                "GET",
                url,
                headers={
                    "Accept": AJAX_ACCEPT,
                    "Referer": SEED_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except LegalCliError as exc:
            raise _with_source_context(exc, stage="filters", fetched_urls=[url]) from exc
        return _rubros_from_response(response)
    finally:
        if owns_client:
            http.close()


def _rubros_from_response(response: httpx.Response) -> list[JsonDict]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise parse_error(
            "Boletin Oficial Nacional rubros response was not valid JSON",
            details=_response_evidence(response, include_body=True),
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        ) from exc
    if not isinstance(payload, list):
        raise parse_error(
            "Boletin Oficial Nacional rubros response was not a list",
            details={"url": str(response.url), "shape": type(payload).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )
    rubros: list[JsonDict] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        rid = _optional_text(entry.get("id"))
        name = _optional_text(entry.get("name")) or rid
        if rid is None and name is None:
            continue
        rubros.append({"id": rid, "name": name})
    return rubros


def handle_filters(args: argparse.Namespace) -> LegalResponse:
    requested = _optional_text(getattr(args, "section", None))
    if requested is not None:
        section = SECTION_ALIASES.get(requested.strip().lower())
        if section not in SECTION_IDS:
            raise usage_error("--section must be primera, segunda, or tercera")
        sections = [section]
    else:
        sections = list(SECTION_IDS)

    fetched_urls: list[str] = []
    rubros_by_section: JsonDict = {}
    with _make_client() as client:
        fetch_seed_page(client=client)
        for section in sections:
            rubros_by_section[section] = fetch_rubros(section=section, client=client)
            fetched_urls.append(RUBROS_URL.format(section=section))

    section_facets = [
        {"id": name, "name": f"{name.capitalize()} seccion", "rubro_count": len(rubros_by_section[name])}
        for name in sections
    ]
    return LegalResponse(
        ok=True,
        source=SOURCE_ID,
        operation="filters",
        request={"section": requested} if requested is not None else {},
        facets={"section": section_facets, "rubros": rubros_by_section},
        provenance=_provenance(
            fetched_urls=fetched_urls,
            raw={"rubro_counts": {name: len(rubros_by_section[name]) for name in sections}},
        ),
        warnings=[],
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    section, notice_id, notice_date = _detail_request_from_args(args)
    with _make_client() as client:
        detail = fetch_detail(
            section=section,
            notice_id=notice_id,
            notice_date=notice_date,
            client=client,
        )

    document = detail_to_document(detail, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={
            "section": section,
            "id": notice_id,
            "date": _compact_date(notice_date),
        },
        document=document,
        provenance=document.provenance,
    )


def fetch_seed_page(*, client: LegalHttpClient | None = None) -> SeedPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        try:
            response = http.request("GET", SEED_URL)
        except LegalCliError as exc:
            raise _with_source_context(exc, stage="seed", fetched_urls=[SEED_URL]) from exc
        return SeedPage(url=str(response.url), html=response.text, headers=_useful_headers(response))
    finally:
        if owns_client:
            http.close()


def fetch_search_page(
    *,
    query: Mapping[str, Any],
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> BoNacionalSearchPage:
    return fetch_search_page_form(
        endpoint=_endpoint_for_query(query),
        form_data=search_form_data(query),
        referer=SEED_URL,
        client=client,
        include_raw=include_raw,
    )


def fetch_search_page_form(
    *,
    endpoint: str,
    form_data: Mapping[str, Any],
    referer: str,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> BoNacionalSearchPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        try:
            response = http.request(
                "POST",
                endpoint,
                data=dict(form_data),
                headers={
                    "Accept": AJAX_ACCEPT,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer": referer,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except LegalCliError as exc:
            raise _with_source_context(exc, stage="search", fetched_urls=[endpoint]) from exc
        return parse_search_response(response, include_raw=include_raw)
    finally:
        if owns_client:
            http.close()


def fetch_detail(
    *,
    section: str,
    notice_id: str,
    notice_date: str,
    client: LegalHttpClient | None = None,
) -> BoNacionalDetail:
    owns_client = client is None
    http = client or _make_client()
    url = detail_url(section=section, notice_id=notice_id, notice_date=notice_date)
    try:
        try:
            response = http.request("GET", url)
        except LegalCliError as exc:
            raise _with_source_context(exc, stage="detail", fetched_urls=[url]) from exc
        html = response.text
        if not html.strip():
            raise not_found(
                "Boletin Oficial Nacional detail page was empty",
                details={"section": section, "id": notice_id, "date": notice_date},
                provenance=_provenance(fetched_urls=[str(response.url)], source_response_id=notice_id),
            )
        return BoNacionalDetail(
            section=section,
            notice_id=notice_id,
            notice_date=notice_date,
            url=str(response.url),
            html=html,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def parse_search_response(response: httpx.Response, *, include_raw: bool = False) -> BoNacionalSearchPage:
    payload = _json_payload(response)
    content = _response_content(payload, response=response)
    html = content.get("html")
    if html is None:
        html = ""
    if not isinstance(html, str):
        raise parse_error(
            "Boletin Oficial Nacional search html field had an unexpected shape",
            details={"url": str(response.url), "shape": type(html).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )

    source_page = _source_page(content)
    items = parse_result_items(html, page_url=str(response.url), include_raw=include_raw)
    total = _total_from_counts(source_page.get("cantidad_result_seccion"))
    if total is None:
        total = len(items)
    return BoNacionalSearchPage(
        url=str(response.url),
        items=items,
        total=total,
        source_page=source_page,
        headers=_useful_headers(response),
    )


def parse_result_items(html: str, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen: set[str] = set()
    current_rubro: str | None = None

    for node in root.iter():
        if _has_class(node, "seccion-rubro"):
            current_rubro = clean_text(node.text())
            continue
        if node.tag != "a":
            continue

        href = node.get("href")
        detail_url = absolute_url(page_url, href)
        match = _DETAIL_RE.search(urlparse(detail_url or "").path)
        if detail_url is None or match is None or _first_by_class(node, "linea-aviso") is None:
            continue

        item = _result_item(
            anchor=node,
            detail_url=detail_url,
            match=match,
            rubro=current_rubro,
            page_url=page_url,
            include_raw=include_raw,
        )
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)

    return items


def search_form_data(query: Mapping[str, Any]) -> JsonDict:
    params = source_params(query)
    return {
        "params": json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "array_volver": "[]",
    }


def source_params(query: Mapping[str, Any]) -> JsonDict:
    section_ids = _section_ids_from_query(query)
    return {
        "texto": query.get("keywords") or "",
        "seccion": section_ids,
        "rubros": [],
        "nroNorma": query.get("number") or "",
        "anioNorma": str(query.get("year") or ""),
        "denominacion": "",
        "comienzaDenominacion": False,
        "ordenamientoSegunda": "",
        "tipoContratacion": "",
        "nroContratacion": "",
        "anioContratacion": "",
        "fechaDesde": _http_date(query.get("from")),
        "fechaHasta": _http_date(query.get("to")),
        "tipoBusqueda": "Avanzada",
        "numeroPagina": 1,
        "ultimoRubro": "",
        "ultimaSeccion": "",
        "ultimoItemExterno": "",
        "ultimoItemInterno": "",
        "todasLasPalabras": query.get("mode") != "any",
        "busquedaOriginal": True,
        "hayMasResultadosBusqueda": False,
        "filtroPorRubrosSeccion": False,
        "filtroPorRubroBusqueda": False,
        "filtroPorSeccionBusqueda": False,
        "seccionesOriginales": section_ids,
    }


def continuation_form_data(
    query: Mapping[str, Any],
    *,
    source_page: Mapping[str, Any],
    fallback_page: int,
) -> JsonDict:
    params = source_params(query)
    params.update(
        {
            "numeroPagina": _continuation_page(source_page, fallback=fallback_page),
            "ultimoRubro": source_page.get("ult_rubro") or "",
            "ultimaSeccion": source_page.get("ult_seccion") or "",
            "busquedaOriginal": False,
            "hayMasResultadosBusqueda": True,
        }
    )
    ultimos_items = source_page.get("ultimos_items")
    if isinstance(ultimos_items, Mapping):
        params["ultimoItemExterno"] = _ultimo_item_name(ultimos_items.get("itemExterno"))
        params["ultimoItemInterno"] = _ultimo_item_name(ultimos_items.get("itemInterno"))
    return {
        "params": json.dumps(params, ensure_ascii=False, separators=(",", ":")),
        "array_volver": "[]",
    }


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("Boletin Oficial Nacional source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation(
        "filters",
        handle_filters,
        help="list Boletin Oficial Nacional section rubros (categories)",
        add_arguments=add_filters_arguments,
    )
    adapter.register_operation(
        "search",
        handle_search,
        help="search Boletin Oficial Nacional advanced search",
        add_arguments=add_search_arguments,
    )
    adapter.register_operation(
        "get",
        handle_get,
        help="fetch Boletin Oficial Nacional detail page",
        add_arguments=add_get_arguments,
    )
    adapter.register_operation(
        "next",
        handle_next,
        help="continue a cached Boletin Oficial Nacional search",
    )
    return adapter


def _result_item(
    *,
    anchor: HtmlNode,
    detail_url: str,
    match: re.Match[str],
    rubro: str | None,
    page_url: str,
    include_raw: bool,
) -> LegalItem:
    line = _first_by_class(anchor, "linea-aviso")
    assert line is not None
    section_slug = match.group("section").lower()
    notice_id = match.group("notice_id")
    notice_date = match.group("date")
    publication_date = _publication_date(line) or _compact_date(notice_date)
    heading = _heading(line)
    details = _detail_texts(line)
    norm_label = _norm_label(details)
    snippet = _snippet(details, norm_label=norm_label)
    document_type = _document_type(norm_label)
    item_id = f"{SOURCE_ID}:{section_slug}:{notice_id}:{notice_date}"
    detail_url = _public_url(detail_url) or detail_url
    title = _title(heading=heading, norm_label=norm_label, notice_id=notice_id)
    highlights = _highlights(line)

    return LegalItem(
        id=item_id,
        title=title,
        date=publication_date,
        document_type=document_type,
        url=detail_url,
        snippet=snippet,
        facets=_compact(
            {
                "section": section_slug,
                "section_id": SECTION_IDS.get(section_slug),
                "rubro": rubro,
            }
        ),
        source_fields=_compact(
            {
                "notice_id": notice_id,
                "notice_date": notice_date,
                "section": section_slug,
                "section_id": SECTION_IDS.get(section_slug),
                "publication_date": publication_date,
                "rubro": rubro,
                "norm_label": norm_label,
                "detail_path": urlparse(detail_url).path,
                "query": _detail_query_fields(detail_url),
                "highlights": highlights,
            }
        ),
        raw={"href": anchor.get("href"), "heading": heading, "details": details} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=item_id,
            raw={"section": section_slug, "notice_id": notice_id, "rubro": rubro},
        ),
    )


def detail_to_document(detail: BoNacionalDetail, *, include_raw: bool = False) -> LegalDocument:
    root = parse_html(detail.html)
    article = root.find("article") or root
    title_node = _node_by_id(root, "tituloDetalleAviso") or article
    body_node = _node_by_id(root, "cuerpoDetalleAviso") or article
    title = _first_tag_text(title_node, "h1")
    norm_label = _first_tag_text(title_node, "h2")
    gde_text = _first_tag_text(title_node, "h6")
    norm_parts = _parse_norm_label(norm_label)
    body = text_content(body_node) or ""
    if not body:
        raise parse_error(
            "Boletin Oficial Nacional detail page did not contain visible body text",
            details={"section": detail.section, "id": detail.notice_id, "date": detail.notice_date},
            provenance=_provenance(fetched_urls=[detail.url], source_response_id=_detail_response_id(detail)),
        )

    publication_date = _detail_publication_date(root) or _compact_date(detail.notice_date)
    links = detail_links(detail.html, page_url=detail.url)
    files = detail_files(detail.html, page_url=detail.url)
    primary_file_url = _primary_file_url(files)
    gde_code = _gde_code(gde_text or body)
    category = _detail_category(root)
    section_heading = _detail_section_heading(root, section=detail.section)
    document_title = _detail_title(title=title, norm_label=norm_label, notice_id=detail.notice_id)
    metadata = _compact(
        {
            "section": detail.section,
            "section_id": SECTION_IDS.get(detail.section),
            "section_heading": section_heading,
            "category": category,
            "title": title,
            "norm_label": norm_label,
            "norm_type": norm_parts.get("type"),
            "norm_number": norm_parts.get("number"),
            "gde_code": gde_code,
            "gde_text": gde_text,
            "publication_date": publication_date,
            "notice_id": detail.notice_id,
            "notice_date": detail.notice_date,
            "pdf_url": primary_file_url,
        }
    )

    return LegalDocument(
        id=_detail_response_id(detail),
        title=document_title,
        date=publication_date,
        document_type=_optional_text(norm_parts.get("type")),
        body=body,
        url=_public_url(detail.url),
        file_url=primary_file_url,
        content_type="text/html",
        text_format="plain_text",
        metadata=metadata,
        links=links,
        files=files,
        source_fields=_compact(
            {
                "notice_id": detail.notice_id,
                "notice_date": detail.notice_date,
                "section": detail.section,
                "section_id": SECTION_IDS.get(detail.section),
                "category": category,
                "norm_label": norm_label,
                "norm_type": norm_parts.get("type"),
                "norm_number": norm_parts.get("number"),
                "gde_code": gde_code,
                "detail_path": urlparse(detail.url).path,
            }
        ),
        raw={"detail_html": detail.html, "headers": detail.headers} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[detail.url],
            source_response_id=_detail_response_id(detail),
            raw={"detail_headers": detail.headers},
        ),
    )


def detail_links(html: str, *, page_url: str) -> list[JsonDict]:
    root = parse_html(html)
    scope = _node_by_id(root, "cuerpoDetalleAviso") or root.find("article") or root
    return _dedupe_links(
        {
            "url": _public_url(str(link["url"])),
            "label": _optional_text(link.get("label")) or str(link["url"]),
            "kind": link.get("kind") or classify_link(str(link["url"]), base_url=page_url),
        }
        for link in extract_links(scope, base_url=page_url)
        if _optional_text(link.get("url"))
    )


def detail_files(html: str, *, page_url: str) -> list[JsonDict]:
    root = parse_html(html)
    files: list[JsonDict] = []
    for node in root.iter():
        onclick = node.get("onclick")
        if not onclick:
            continue
        for match in _PDF_AVISO_RE.finditer(onclick):
            url = absolute_url(page_url, match.group("path"))
            if url:
                files.append(
                    {
                        "url": _public_url(url),
                        "label": "texto publicado pdf",
                        "kind": "pdf",
                        "target_type": "published_pdf",
                        "method": "GET",
                    }
                )
        for match in _DOWNLOAD_AVISO_RE.finditer(onclick):
            args = _JS_QUOTED_ARG_RE.findall(match.group("args"))
            if len(args) < 4:
                continue
            endpoint = absolute_url(page_url, args[3])
            if endpoint is None:
                continue
            files.append(
                {
                    "url": _public_url(endpoint),
                    "label": "texto publicado pdf download endpoint",
                    "kind": "data",
                    "target_type": "published_pdf_download_endpoint",
                    "method": "POST",
                    "fields": {
                        "nombreSeccion": args[0],
                        "idAviso": args[1],
                        "fechaPublicacion": args[2],
                    },
                }
            )
    return _dedupe_links(files)


def _query_from_args(args: argparse.Namespace) -> JsonDict:
    section = _section_arg(args.section)
    section_ids = [SECTION_IDS[item] for item in SECTION_IDS] if section == "all" else [SECTION_IDS[section]]
    query: JsonDict = {
        "section": section,
        "section_ids": section_ids,
        "keywords": _optional_text(args.keywords),
        "mode": _mode_arg(args.mode),
        "from": _iso_date_arg(args.date_from, flag="--from"),
        "to": _iso_date_arg(args.date_to, flag="--to"),
        "number": _digits_arg(args.number, flag="--number"),
        "year": _year_arg(args.year),
    }
    return {key: value for key, value in query.items() if value not in (None, "", [])}


def _detail_request_from_args(args: argparse.Namespace) -> tuple[str, str, str]:
    canonical = _canonical_detail_id(args.notice_id)
    raw_section = args.section or canonical.get("section")
    raw_notice_id = canonical.get("notice_id") or _optional_text(args.notice_id)
    raw_date = args.date or canonical.get("date")
    section = _detail_section_arg(raw_section)
    notice_id = _notice_id_arg(raw_notice_id)
    notice_date = _notice_date_arg(raw_date)
    if canonical and canonical.get("section") and canonical["section"] != section:
        raise usage_error("canonical Boletin Oficial id section does not match --section")
    if canonical and canonical.get("date") and canonical["date"] != notice_date:
        raise usage_error("canonical Boletin Oficial id date does not match --date")
    return section, notice_id, notice_date


def _canonical_detail_id(value: Any) -> JsonDict:
    text = _optional_text(value)
    if not text or not text.startswith(f"{SOURCE_ID}:"):
        return {}
    parts = text.split(":")
    if len(parts) != 4:
        raise usage_error("Boletin Oficial Nacional canonical id must be bo-nacional:<section>:<id>:<yyyymmdd>")
    return {
        "section": _detail_section_arg(parts[1]),
        "notice_id": _notice_id_arg(parts[2]),
        "date": _notice_date_arg(parts[3]),
    }


def _detail_section_arg(value: Any) -> str:
    section = _section_arg(value)
    if section == "all":
        raise usage_error("Boletin Oficial Nacional detail requires --section primera, segunda, or tercera")
    return section


def _notice_id_arg(value: Any) -> str:
    text = _optional_text(value)
    if not text or not text.isdigit():
        raise usage_error("Boletin Oficial Nacional --id must be numeric", details={"id": value})
    return text


def _notice_date_arg(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        raise usage_error("Boletin Oficial Nacional get requires --date")
    if len(text) == 8 and text.isdigit():
        return text
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise usage_error("--date must be an ISO date YYYY-MM-DD") from exc
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


def _query_from_record(record: SearchCacheRecord) -> JsonDict:
    query = record.cursor_payload.get("query")
    if not isinstance(query, Mapping):
        raise parse_error(
            "cached Boletin Oficial Nacional search state is missing query metadata",
            details={"search_id": record.search_id},
        )
    return {str(key): value for key, value in query.items() if value not in (None, "")}


def _cached_page_items(record: SearchCacheRecord) -> list[JsonDict]:
    value = record.cursor_payload.get("available_items")
    if value is None:
        return []
    if not isinstance(value, list):
        raise parse_error(
            "cached Boletin Oficial Nacional search state has malformed available items",
            details={"search_id": record.search_id},
        )
    items: list[JsonDict] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise parse_error(
                "cached Boletin Oficial Nacional search state has malformed available items",
                details={"search_id": record.search_id},
            )
        items.append({str(key): item_value for key, item_value in item.items()})
    return items


def _cached_next_response(
    *,
    record: SearchCacheRecord,
    query: Mapping[str, Any],
    limit: int,
    items: list[Any],
    new_returned_count: int,
) -> LegalResponse:
    state = dict(record.cursor_payload)
    state["limit"] = limit
    state["returned_count"] = new_returned_count
    page_start_offset = _int_from_state(state, "page_start_offset") or 0
    total = _int_from_state(state, "total")
    has_more = _has_more(
        total=total,
        page_start_offset=page_start_offset,
        available_count=len(_cached_page_items(record)),
        returned_count=new_returned_count,
        source_exhausted=bool(state.get("source_exhausted")),
    )
    save_search_state(
        source=SOURCE_ID,
        query={**query, "limit": limit},
        search_id=record.search_id,
        cookies=record.cookies,
        store_cookies=True,
        hidden_fields=record.hidden_fields,
        cursor_payload=state,
        raw_provenance=record.raw_provenance,
    )
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=page_start_offset + new_returned_count - len(items),
            page=_int_from_state(state, "current_page"),
            total=total,
            has_more=has_more,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=_cached_fetched_urls(record),
            raw={
                "from_cache": True,
                "search_id": record.search_id,
                "page_start_offset": page_start_offset,
            },
        ),
        facets={
            "sections": dict(SECTION_IDS),
            "keyword_modes": {"all": "todas las palabras", "any": "alguna de las palabras"},
        },
    )


def _empty_next_response(*, record: SearchCacheRecord, query: Mapping[str, Any], limit: int) -> LegalResponse:
    state = record.cursor_payload
    page_start_offset = _int_from_state(state, "page_start_offset") or 0
    returned_count = _int_from_state(state, "returned_count") or 0
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=[],
        page=PageInfo(
            limit=limit,
            offset=page_start_offset + returned_count,
            page=_int_from_state(state, "current_page"),
            total=_int_from_state(state, "total"),
            has_more=False,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=_cached_fetched_urls(record),
            raw={"from_cache": True, "source_exhausted": True, "search_id": record.search_id},
        ),
    )


def _validate_search_record(record: SearchCacheRecord) -> None:
    if record.source != SOURCE_ID:
        raise usage_error(
            "search id belongs to a different source",
            details={"search_id": record.search_id, "source": record.source},
        )
    if not isinstance(record.cursor_payload, Mapping):
        raise parse_error(
            "cached Boletin Oficial Nacional search state is malformed",
            details={"search_id": record.search_id},
        )


def _required_search_id(value: Any) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error("Boletin Oficial Nacional next requires --search-id")
    return text


def _mapping_from_state(state: Mapping[str, Any], key: str) -> JsonDict:
    value = state.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise parse_error("cached Boletin Oficial Nacional search state is malformed", details={"field": key})
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _text_from_state(state: Mapping[str, Any], key: str) -> str | None:
    return _optional_text(state.get(key))


def _int_from_state(state: Mapping[str, Any], key: str) -> int | None:
    value = state.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise parse_error("cached Boletin Oficial Nacional search state is malformed", details={"field": key})
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError as exc:
        raise parse_error("cached Boletin Oficial Nacional search state is malformed", details={"field": key}) from exc


def _cached_fetched_urls(record: SearchCacheRecord) -> list[str]:
    value = record.raw_provenance.get("fetched_urls")
    if isinstance(value, list):
        urls = [url for url in (_optional_text(item) for item in value) if url]
        if urls:
            return urls
    result_url = _text_from_state(record.cursor_payload, "result_url")
    return [result_url] if result_url else [SEARCH_URL]


def _save_search_state(
    *,
    client: LegalHttpClient,
    query: Mapping[str, Any],
    limit: int,
    form_data: Mapping[str, Any],
    search_page: BoNacionalSearchPage,
    returned_items: list[LegalItem],
    search_id: str | None = None,
    fetched_urls: list[str] | None = None,
    page_start_offset: int = 0,
    current_page: int = 1,
    total: int | None = None,
) -> str:
    effective_total = total if total is not None else search_page.total
    record = save_search_state(
        source=SOURCE_ID,
        query={**query, "limit": limit},
        search_id=search_id,
        cookies=client.cookies.jar,
        store_cookies=True,
        hidden_fields={},
        cursor_payload={
            "query": dict(query),
            "limit": limit,
            "form_data": dict(form_data),
            "endpoint": _endpoint_for_query(query),
            "result_url": search_page.url,
            "current_page": current_page,
            "next_page": _continuation_page(search_page.source_page, fallback=current_page + 1),
            "total": effective_total,
            "source_page": search_page.source_page,
            "available_count": len(search_page.items),
            "available_items": [item.to_dict() for item in search_page.items],
            "returned_count": len(returned_items),
            "page_start_offset": page_start_offset,
            "source_exhausted": not search_page.items,
        },
        raw_provenance={
            "source_map": SOURCE_MAP,
            "fetched_urls": fetched_urls or [search_page.url],
            "headers": {"result": search_page.headers},
        },
    )
    return record.search_id


def restore_cookies(*, client: LegalHttpClient, cookies: list[JsonDict]) -> None:
    for cookie in cookies:
        name = _optional_text(cookie.get("name"))
        value = _optional_text(cookie.get("value"))
        if name is None or value is None:
            continue
        kwargs: JsonDict = {}
        domain = _optional_text(cookie.get("domain"))
        path = _optional_text(cookie.get("path"))
        if domain:
            kwargs["domain"] = domain
        if path:
            kwargs["path"] = path
        client.cookies.set(name, value, **kwargs)


def _json_payload(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise parse_error(
            "Boletin Oficial Nacional search response was not valid JSON",
            details=_response_evidence(response, include_body=True),
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        ) from exc
    if not isinstance(payload, Mapping):
        raise parse_error(
            "Boletin Oficial Nacional search response was not an object",
            details={"url": str(response.url), "shape": type(payload).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )
    return payload


def _response_content(payload: Mapping[str, Any], *, response: httpx.Response) -> Mapping[str, Any]:
    if payload.get("error") not in (None, 0, False, "0"):
        raise parse_error(
            "Boletin Oficial Nacional search returned a source error",
            details={
                "url": str(response.url),
                "error": payload.get("error"),
                "mensajes": payload.get("mensajes"),
            },
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )
    content = payload.get("content")
    if not isinstance(content, Mapping):
        raise parse_error(
            "Boletin Oficial Nacional search content field had an unexpected shape",
            details={"url": str(response.url), "shape": type(content).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )
    return content


def _source_page(content: Mapping[str, Any]) -> JsonDict:
    page: JsonDict = {}
    counts = _counts(content.get("cantidad_result_seccion"))
    if counts is not None:
        page["cantidad_result_seccion"] = counts
    sig_pag = _parse_int(content.get("sig_pag"))
    if sig_pag is not None:
        page["sig_pag"] = sig_pag
    for key in ("ult_seccion", "ult_rubro"):
        text = _optional_text(content.get(key))
        if text is not None:
            page[key] = text
    ultimos_items = content.get("ultimos_items")
    if isinstance(ultimos_items, Mapping):
        page["ultimos_items"] = dict(ultimos_items)
    elif isinstance(ultimos_items, list):
        page["ultimos_items"] = list(ultimos_items)
    return page


def _heading(line: HtmlNode) -> str | None:
    node = _first_by_class(line, "item")
    return clean_text(node.text() if node is not None else None)


def _detail_texts(line: HtmlNode) -> list[str]:
    details: list[str] = []
    for node in line.iter("p"):
        if not _has_class(node, "item-detalle"):
            continue
        text = clean_text(node.text())
        if text:
            details.append(text)
    return details


def _publication_date(line: HtmlNode) -> str | None:
    for detail in _detail_texts(line):
        match = _PUBLICATION_DATE_RE.search(detail)
        if match:
            return normalize_date(match.group("date"))
    return None


def _norm_label(details: Sequence[str]) -> str | None:
    for detail in details:
        if _PUBLICATION_DATE_RE.search(detail):
            continue
        if detail:
            return detail
    return None


def _snippet(details: Sequence[str], *, norm_label: str | None) -> str | None:
    snippet_parts = [
        detail
        for detail in details
        if detail != norm_label and _PUBLICATION_DATE_RE.search(detail) is None
    ]
    return clean_snippet(" ".join(snippet_parts), max_length=SNIPPET_LENGTH)


def _document_type(norm_label: str | None) -> str | None:
    text = _optional_text(norm_label)
    if text is None:
        return None
    match = _NORM_TYPE_RE.search(text)
    return clean_text(match.group("kind")) if match else text


def _title(*, heading: str | None, norm_label: str | None, notice_id: str) -> str:
    if heading and norm_label:
        return f"{norm_label} - {heading}"
    if heading:
        return heading
    if norm_label:
        return norm_label
    return f"Aviso {notice_id}"


def _highlights(node: HtmlNode) -> list[str]:
    highlights: list[str] = []
    seen: set[str] = set()
    for span in node.iter("span"):
        style = (span.get("style") or "").casefold()
        if "background" not in style:
            continue
        text = _optional_text(span.text())
        if text is None:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        highlights.append(text)
    return highlights


def _detail_query_fields(detail_url: str) -> JsonDict:
    parsed = urlparse(detail_url)
    fields = {
        key: values[-1]
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        if values
    }
    return dict(sorted(fields.items()))


def _endpoint_for_query(query: Mapping[str, Any]) -> str:
    return SEARCH_SECOND_URL if query.get("section") == "segunda" else SEARCH_URL


def _section_ids_from_query(query: Mapping[str, Any]) -> list[int]:
    raw = query.get("section_ids")
    if isinstance(raw, list) and all(isinstance(item, int) for item in raw):
        return list(raw)
    section = query.get("section")
    if section == "all" or section is None:
        return list(SECTION_IDS.values())
    return [SECTION_IDS[str(section)]]


def _section_arg(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return "all"
    key = _key(text)
    section = SECTION_ALIASES.get(key)
    if section is None:
        raise usage_error(
            "unknown Boletin Oficial Nacional section",
            details={"section": text, "known_sections": ["primera", "segunda", "tercera", "all"]},
        )
    return section


def _mode_arg(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return "all"
    mode = MODE_ALIASES.get(_key(text))
    if mode is None:
        raise usage_error(
            "unknown Boletin Oficial Nacional keyword mode",
            details={"mode": text, "known_modes": ["all", "any"]},
        )
    return mode


def _iso_date_arg(value: Any, *, flag: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise usage_error(f"{flag} must be an ISO date YYYY-MM-DD") from exc
    return text


def _year_arg(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parsed = _digits_arg(text, flag="--year")
    if parsed is None or len(parsed) != 4:
        raise usage_error("--year must be a 4 digit year")
    return parsed


def _digits_arg(value: Any, *, flag: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if not text.isdigit():
        raise usage_error(f"{flag} must contain only digits")
    return text


def _http_date(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return ""
    parsed = date.fromisoformat(text)
    return f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}"


def _compact_date(value: str) -> str | None:
    if len(value) != 8 or not value.isdigit():
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def _counts(value: Any) -> JsonDict | None:
    if not isinstance(value, Mapping):
        return None
    counts: JsonDict = {}
    for key, item in value.items():
        parsed = _parse_int(item)
        if parsed is not None:
            counts[str(key)] = parsed
    return counts or None


def _total_from_counts(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    totals = [item for item in value.values() if isinstance(item, int)]
    return sum(totals) if totals else None


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _optional_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {str(key): item for key, item in value.items() if item not in (None, "", [], {})}


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def _iter_by_class(root: HtmlNode, class_name: str) -> list[HtmlNode]:
    return [node for node in root.iter() if _has_class(node, class_name)]


def _first_by_class(root: HtmlNode, class_name: str) -> HtmlNode | None:
    return next(iter(_iter_by_class(root, class_name)), None)


def _has_class(node: HtmlNode, class_name: str) -> bool:
    return class_name in (node.get("class") or "").split()


def _source_response_id(query: Mapping[str, Any]) -> str:
    section = query.get("section") or "all"
    keyword = _slug(query.get("keywords") or "all")
    date_part = query.get("from") or query.get("to") or "open"
    return f"{SOURCE_ID}:{section}:{date_part}:{keyword}"


def _detail_response_id(detail: BoNacionalDetail) -> str:
    return f"{SOURCE_ID}:{detail.section}:{detail.notice_id}:{detail.notice_date}"


def detail_url(*, section: str, notice_id: str, notice_date: str) -> str:
    return f"{BASE_URL}/detalleAviso/{section}/{notice_id}/{notice_date}"


def _detail_title(*, title: str | None, norm_label: str | None, notice_id: str) -> str:
    if norm_label and title:
        return f"{norm_label} - {title}"
    return title or norm_label or f"Aviso {notice_id}"


def _parse_norm_label(value: str | None) -> JsonDict:
    text = _optional_text(value)
    if text is None:
        return {}
    match = _NORM_PARTS_RE.match(text)
    if match:
        return {
            "type": clean_text(match.group("kind")),
            "number": clean_text(match.group("number")),
        }
    return {"type": _document_type(text), "label": text}


def _gde_code(value: str | None) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    match = _GDE_CODE_RE.search(text)
    return clean_text(match.group("code")) if match else None


def _detail_publication_date(root: HtmlNode) -> str | None:
    text = text_content(root) or ""
    match = _DETAIL_PUBLICATION_DATE_RE.search(text)
    return normalize_date(match.group("date")) if match else None


def _detail_category(root: HtmlNode) -> str | None:
    for anchor in root.iter("a"):
        href = anchor.get("href") or ""
        if "rubro=" not in href:
            continue
        text = anchor.text()
        if text:
            return text
    return None


def _detail_section_heading(root: HtmlNode, *, section: str) -> str | None:
    section_key = _key(section)
    for node in root.iter("h2"):
        text = node.text()
        if text and "seccion" in _key(text) and section_key in _key(text):
            return text
    return None


def _node_by_id(root: HtmlNode, node_id: str) -> HtmlNode | None:
    for node in root.iter():
        if node.get("id") == node_id:
            return node
    return None


def _first_tag_text(node: HtmlNode, tag: str) -> str | None:
    found = node.find(tag)
    return found.text() if found is not None else None


def _dedupe_links(links: Iterable[Mapping[str, Any]]) -> list[JsonDict]:
    output: list[JsonDict] = []
    seen: set[tuple[str, str | None]] = set()
    for link in links:
        url = _optional_text(link.get("url"))
        if url is None:
            continue
        target_type = _optional_text(link.get("target_type"))
        key = (url, target_type)
        if key in seen:
            continue
        seen.add(key)
        output.append({str(item_key): item for item_key, item in link.items() if item not in (None, "", [], {})})
    return output


def _primary_file_url(files: Sequence[Mapping[str, Any]]) -> str | None:
    for file in files:
        url = _optional_text(file.get("url"))
        if url and file.get("kind") == "pdf" and file.get("method") == "GET":
            return url
    for file in files:
        url = _optional_text(file.get("url"))
        if url:
            return url
    return None


def _continuation_page(source_page: Mapping[str, Any], *, fallback: int) -> int:
    parsed = _parse_int(source_page.get("sig_pag"))
    return parsed if parsed is not None and parsed >= 1 else fallback


def _ultimo_item_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _optional_text(value.get("nombre"))


def _has_more(
    *,
    total: int | None,
    page_start_offset: int,
    available_count: int,
    returned_count: int,
    source_exhausted: bool,
) -> bool:
    if returned_count < available_count:
        return True
    if source_exhausted:
        return False
    if total is not None:
        return page_start_offset + returned_count < total
    return False


def _slug(value: Any) -> str:
    text = _optional_text(value) or "all"
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return normalized or "all"


def _make_client() -> LegalHttpClient:
    resolve_ips = _resolve_ips()
    resolve_host = urlparse(BASE_URL).hostname if resolve_ips else None
    return BoNacionalHttpClient(
        headers={"Referer": SEED_URL},
        resolve_host=resolve_host,
        resolve_ips=resolve_ips,
    )


def _resolve_ips() -> tuple[str, ...]:
    raw = os.environ.get(RESOLVE_ENV)
    if raw is None:
        return DEFAULT_RESOLVE_IPS
    if raw.strip().casefold() in {"", "system", "dns", "none", "off"}:
        return ()
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(values)


@contextmanager
def _dns_override(host: str | None, ips: Sequence[str]) -> Iterator[None]:
    if not host or not ips:
        yield
        return

    original_getaddrinfo = socket.getaddrinfo
    target = host.casefold()

    def getaddrinfo(
        query_host: str | bytes | None,
        port: str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        text_host = query_host.decode("ascii", "ignore") if isinstance(query_host, bytes) else query_host
        if text_host is not None and text_host.casefold() == target:
            results: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
            for ip in ips:
                results.extend(original_getaddrinfo(ip, port, family, type, proto, flags))
            return results
        return original_getaddrinfo(query_host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _dns_evidence() -> JsonDict:
    ips = _resolve_ips()
    if not ips:
        return {"mode": "system"}
    return {"mode": "override", "host": urlparse(BASE_URL).hostname, "ips": list(ips), "env": RESOLVE_ENV}


def _with_source_context(error: LegalCliError, *, stage: str, fetched_urls: list[str]) -> LegalCliError:
    if error.code not in {"network_error", "source_unavailable"}:
        return error
    details = dict(error.details or {})
    details.update(
        {
            "source": SOURCE_ID,
            "stage": stage,
            "source_map": SOURCE_MAP,
            "dns": _dns_evidence(),
        }
    )
    return LegalCliError(
        code=error.code,
        message=error.message,
        retryable=error.retryable,
        capability_required=error.capability_required,
        details=details,
        provenance=error.provenance or _provenance(fetched_urls=fetched_urls),
    )


def _useful_headers(response: httpx.Response) -> JsonDict:
    allowed = {"content-type", "etag", "last-modified", "location", "retry-after"}
    return {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in allowed
    }


def _response_evidence(response: httpx.Response, *, include_body: bool = False) -> JsonDict:
    evidence: JsonDict = {
        "url": str(response.url),
        "method": response.request.method,
        "status_code": response.status_code,
    }
    useful_headers = _useful_headers(response)
    if useful_headers:
        evidence["headers"] = useful_headers
    if include_body:
        text = response.text.strip()
        if text:
            evidence["body_snippet"] = text[:500]
    return evidence


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[SEED_URL, SEARCH_URL, SEARCH_SECOND_URL],
        fetched_urls=[_public_url(url) or url for url in fetched_urls],
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _public_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() != "www.boletinoficial.gob.ar":
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, parsed.fragment))


register_adapter(build_adapter(), replace=True)
