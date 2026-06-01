"""Normas PBA direct search/detail adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from legal import enrichment
from legal.errors import not_found, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import build_page_info, decode_cursor
from legal.parsing import (
    HtmlNode,
    absolute_url,
    classify_link,
    clean_snippet,
    clean_text,
    normalize_date,
    parse_html,
    text_content,
)
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "normas-pba"
SOURCE_MAP = "apps/legal/docs/normas_pba.md"

BASE_URL = "https://normas.gba.gob.ar"
HOME_URL = f"{BASE_URL}/"
SEARCH_URL = f"{BASE_URL}/resultados"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 420

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TOTAL_RE = re.compile(
    r"P[aá]gina\s+(?P<page>\d+)\s+de\s+(?P<total>[\d.]+)\s+resultados",
    re.IGNORECASE,
)
_DETAIL_ROUTE_RE = re.compile(
    r"^/ar-b/(?P<type>[^/]+)/(?P<year>\d{4})/(?P<number>[^/]+)/(?P<internal_id>[^/?#]+)$",
    re.IGNORECASE,
)
_LAST_UPDATE_RE = re.compile(
    r"[UÚ]ltima\s+actualizaci[oó]n:\s*(?P<value>\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2})?)",
    re.IGNORECASE,
)

TYPE_RAW_BY_KEY: Mapping[str, str] = {
    "law": "Law",
    "ley": "Law",
    "decree law": "DecreeLaw",
    "decreto ley": "DecreeLaw",
    "decree": "Decree",
    "decreto": "Decree",
    "resolution": "Resolution",
    "resolucion": "Resolution",
    "disposition": "Disposition",
    "disposicion": "Disposition",
    "general ordinance": "GeneralOrdinance",
    "ordenanza general": "GeneralOrdinance",
    "joint resolution": "JointResolution",
    "resolucion conjunta": "JointResolution",
}

SORT_RAW_BY_KEY: Mapping[str, str] = {
    "publication desc": "by_publication_date_desc",
    "published desc": "by_publication_date_desc",
    "by publication date desc": "by_publication_date_desc",
    "updated desc": "by_updated_at_desc",
    "by updated at desc": "by_updated_at_desc",
    "match desc": "by_match_desc",
    "relevance": "by_match_desc",
    "by match desc": "by_match_desc",
    "number desc": "by_number_desc",
    "by number desc": "by_number_desc",
    "number asc": "by_number_asc",
    "by number asc": "by_number_asc",
    "year asc": "by_year_asc",
    "by year asc": "by_year_asc",
    "year desc": "by_year_desc",
    "by year desc": "by_year_desc",
}
SORT_VALUES = set(SORT_RAW_BY_KEY.values())

FIELD_KEY_BY_LABEL: Mapping[str, str] = {
    "fecha de promulgacion": "promulgation_date",
    "fecha de publicacion": "publication_date",
    "numero de boletin oficial": "bulletin_number",
    "tipo de publicacion": "publication_type",
    "ultima actualizacion": "last_update",
}


@dataclass(frozen=True)
class DetailRoute:
    path: str
    type_slug: str
    year: str
    number: str
    internal_id: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}{self.path}"

    @property
    def item_id(self) -> str:
        return f"{SOURCE_ID}:{self.type_slug}:{self.year}:{self.number}:{self.internal_id}"

    def to_dict(self) -> JsonDict:
        return {
            "path": self.path,
            "type": self.type_slug,
            "year": self.year,
            "number": self.number,
            "internal_id": self.internal_id,
            "url": self.url,
        }


@dataclass(frozen=True)
class NormasSearchPage:
    url: str
    html: str
    items: list[LegalItem]
    page: int | None
    total: int | None
    has_next_page: bool
    headers: JsonDict


@dataclass(frozen=True)
class NormasDetailPage:
    route: DetailRoute
    url: str
    html: str
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", "--phrase", dest="text", help="exact phrase search")
    parser.add_argument("--some-words", dest="some_words", help="search terms where some words may match")
    parser.add_argument("--without-words", dest="without_words", help="terms to exclude")
    parser.add_argument("--type", dest="norm_type", help="friendly norm type, e.g. law or disposition")
    parser.add_argument("--raw-type", help="raw Normas PBA q[terms][raw_type] value")
    parser.add_argument("--number", help="norm number")
    parser.add_argument("--year", type=_year, help="norm year")
    parser.add_argument(
        "--from-date",
        "--published-from",
        dest="published_from",
        help="publication date lower bound, YYYY-MM-DD",
    )
    parser.add_argument(
        "--to-date",
        "--published-to",
        dest="published_to",
        help="publication date upper bound, YYYY-MM-DD",
    )
    parser.add_argument("--bulletin-number", help="Boletin Oficial number")
    parser.add_argument("--sort", help="sort alias or raw Normas PBA q[sort] value")
    parser.add_argument("--page", type=_page_number, help="1-based result page to fetch")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="canonical detail path or full Normas PBA detail URL")


def add_related_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="canonical detail path or full Normas PBA detail URL")


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", help="canonical detail path or full Normas PBA detail URL")
    parser.add_argument(
        "--target",
        choices=("original_text", "fundamentos", "pdf"),
        help="which attached PDF to download (default: the original/first PDF)",
    )
    enrichment.add_text_arguments(parser)


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    requested_page = int(getattr(args, "page", None) or cursor_payload.get("page") or 1)
    offset = int(cursor_payload.get("offset") or 0)

    with _make_client() as client:
        search_page = fetch_search_page(
            query=query,
            page=requested_page,
            client=client,
            include_raw=bool(args.raw),
        )

    items = search_page.items[offset : offset + limit]
    next_page, next_offset, has_more = _next_page_state(
        search_page=search_page,
        current_page=requested_page,
        offset=offset,
        limit=limit,
        returned_count=len(items),
    )
    page_info = build_page_info(
        source=SOURCE_ID,
        operation="search",
        limit=limit,
        offset=offset,
        page=search_page.page or requested_page,
        total=search_page.total,
        item_count=len(items),
        has_more=has_more,
        next_page=next_page,
        next_offset=next_offset,
        raw={"query": query} if has_more else None,
    )

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "page": requested_page, "offset": offset, "limit": limit},
        items=items,
        page=page_info,
        provenance=_provenance(
            fetched_urls=[search_page.url],
            raw={
                "headers": search_page.headers,
                "result_count": len(search_page.items),
                "has_next_page": search_page.has_next_page,
            },
        ),
        facets={"types": dict(TYPE_RAW_BY_KEY), "sorts": sorted(SORT_VALUES)},
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    route = _required_route(args.path)

    with _make_client() as client:
        detail = fetch_detail(route, client=client)

    document = detail_to_document(detail, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={"path": route.path},
        document=document,
        provenance=document.provenance,
    )


def _select_pdf_file(files: list[JsonDict], *, target: str | None) -> JsonDict | None:
    pdfs = [f for f in files if f.get("kind") == "pdf" or str(f.get("url", "")).lower().endswith(".pdf")]
    if not pdfs:
        return None
    if target is not None:
        for f in pdfs:
            if f.get("target_type") == target:
                return f
        return None
    # default preference: original text PDF, then any pdf-typed, then first pdf
    for preferred in ("original_text", "pdf"):
        for f in pdfs:
            if f.get("target_type") == preferred:
                return f
    return pdfs[0]


def handle_download(args: argparse.Namespace) -> LegalResponse:
    route = _required_route(args.path)
    target = _optional_text(getattr(args, "target", None))

    with _make_client() as client:
        detail = fetch_detail(route, client=client)
        files = parse_document_files(detail.html, page_url=detail.url)
        pdf_file = _select_pdf_file(files, target=target)
        if pdf_file is None:
            raise not_found(
                "Normas PBA detail page exposed no downloadable PDF"
                + (f" for target {target}" if target else ""),
                details={"path": route.path, "files": files},
                provenance=_provenance(fetched_urls=[detail.url], source_response_id=route.item_id),
            )
        pdf_url = str(pdf_file["url"])
        response = client.request("GET", pdf_url)
        pdf_bytes = response.content

    pdf_meta = enrichment.finalize_document(
        pdf_bytes,
        want_text=bool(getattr(args, "want_text", False)),
        save_path=_optional_text(getattr(args, "save_pdf", None)),
    )
    document = LegalDocument(
        id=route.item_id,
        title=_route_title(route),
        document_type=_document_type_from_route(route),
        body=pdf_meta.get("text") if bool(getattr(args, "want_text", False)) else None,
        url=route.url,
        file_url=pdf_url,
        content_type="application/pdf",
        text_format="plain_text" if bool(getattr(args, "want_text", False)) else None,
        metadata={
            "path": route.path,
            "target_type": pdf_file.get("target_type"),
            "label": pdf_file.get("label"),
            **pdf_meta,
        },
        files=files,
        source_fields={"route": route.to_dict(), "path": route.path},
        raw={"headers": detail.headers} if bool(args.raw) else {},
        provenance=_provenance(
            fetched_urls=[detail.url, pdf_url],
            source_response_id=route.item_id,
        ),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request=_compact({"path": route.path, "target": target}),
        document=document,
        provenance=document.provenance,
    )


def handle_related(args: argparse.Namespace) -> LegalResponse:
    route = _required_route(args.path)
    limit = int(args.limit or DEFAULT_LIMIT)

    with _make_client() as client:
        detail = fetch_detail(route, client=client)

    related_items = parse_related_items(detail.html, page_url=detail.url, include_raw=bool(args.raw))
    items = related_items[:limit]
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="related",
        query={"path": route.path, "limit": limit},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=0,
            page=1,
            total=len(related_items),
            has_more=len(items) < len(related_items),
        ),
        provenance=_provenance(
            fetched_urls=[detail.url],
            source_response_id=route.item_id,
            raw={"headers": detail.headers},
        ),
    )


def fetch_search_page(
    *,
    query: Mapping[str, Any],
    page: int,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> NormasSearchPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        params = search_params(query, page=page)
        response = http.request("GET", SEARCH_URL, params=params)
        return parse_search_response(response, include_raw=include_raw)
    finally:
        if owns_client:
            http.close()


def fetch_detail(route: DetailRoute, *, client: LegalHttpClient | None = None) -> NormasDetailPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", route.url)
        html = response.text
        if not html.strip():
            raise not_found(
                "Normas PBA detail page was empty",
                details={"path": route.path},
                provenance=_provenance(fetched_urls=[str(response.url)], source_response_id=route.item_id),
            )
        return NormasDetailPage(
            route=route,
            url=str(response.url),
            html=html,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def parse_search_response(response: httpx.Response, *, include_raw: bool = False) -> NormasSearchPage:
    html = response.text
    page_url = str(response.url)
    page, total = parse_result_count(html)
    items = parse_search_items(html, page_url=page_url, include_raw=include_raw)
    if total is None and items:
        total = len(items)
    return NormasSearchPage(
        url=page_url,
        html=html,
        items=items,
        page=page,
        total=total,
        has_next_page=parse_has_next_page(html, page_url=page_url),
        headers=_useful_headers(response),
    )


def parse_search_items(html: str, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen: set[str] = set()
    for index, card in enumerate(_iter_by_class(root, "rule-card"), start=1):
        item = _card_to_item(card, page_url=page_url, index=index, include_raw=include_raw)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def parse_result_count(html: str) -> tuple[int | None, int | None]:
    root = parse_html(html)
    for node in _iter_by_class(root, "total"):
        text = node.text()
        if not text:
            continue
        match = _TOTAL_RE.search(text)
        if match:
            return int(match.group("page")), _parse_int(match.group("total"))
    return None, None


def parse_has_next_page(html: str, *, page_url: str) -> bool:
    root = parse_html(html)
    for anchor in root.iter("a"):
        href = anchor.get("href")
        rel = _optional_text(anchor.get("rel"))
        label = _static_key(anchor.text())
        if rel == "next" or "siguiente" in label:
            return absolute_url(page_url, href) is not None
    return False


def detail_to_document(detail: NormasDetailPage, *, include_raw: bool = False) -> LegalDocument:
    root = parse_html(detail.html)
    rule = _node_by_id(root, "rule-show") or root
    body = text_content(rule)
    if not body:
        raise parse_error(
            "Normas PBA detail page did not contain #rule-show text",
            details={"path": detail.route.path},
            provenance=_provenance(fetched_urls=[detail.url], source_response_id=detail.route.item_id),
        )

    fields = _field_pairs(rule)
    metadata = detail_metadata(rule=rule, route=detail.route, fields=fields, body=body)
    files = parse_document_files(rule, page_url=detail.url)
    related = parse_related_items(detail.html, page_url=detail.url, include_raw=include_raw)
    links = [*_file_links(files), *[_related_item_link(item) for item in related]]
    publication_date = _optional_text(metadata.get("publication_date"))

    return LegalDocument(
        id=detail.route.item_id,
        title=_first_id_text(rule, "rule-name") or _route_title(detail.route),
        date=publication_date,
        document_type=_document_type_from_route(detail.route),
        body=body,
        url=detail.route.url,
        content_type="text/html",
        text_format="plain_text",
        metadata=metadata,
        links=links,
        files=files,
        source_fields={
            "route": detail.route.to_dict(),
            "path": detail.route.path,
            "type": detail.route.type_slug,
            "year": detail.route.year,
            "number": detail.route.number,
            "internal_id": detail.route.internal_id,
            "fields": fields,
        },
        raw={"html": detail.html, "headers": detail.headers} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[detail.url],
            source_response_id=detail.route.item_id,
            raw={"headers": detail.headers},
        ),
    )


def detail_metadata(
    *,
    rule: HtmlNode,
    route: DetailRoute,
    fields: Mapping[str, str],
    body: str,
) -> JsonDict:
    normalized_fields = _normalized_fields(fields)
    sections = _section_texts(rule)
    metadata: JsonDict = {
        "path": route.path,
        "type": route.type_slug,
        "year": route.year,
        "number": route.number,
        "internal_id": route.internal_id,
        **normalized_fields,
        "summary": sections.get("resumen"),
        "observations": sections.get("observaciones"),
        "last_update": normalized_fields.get("last_update") or _last_update(body),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [])}


def parse_document_files(rule: HtmlNode | str, *, page_url: str) -> list[JsonDict]:
    root = parse_html(rule) if isinstance(rule, str) else rule
    files: list[JsonDict] = []
    seen: set[str] = set()
    for container in _iter_by_class(root, "rule-download-links"):
        for anchor in container.iter("a"):
            url = absolute_url(page_url, anchor.get("href"))
            if url is None or url in seen:
                continue
            seen.add(url)
            label = _anchor_label(anchor, url=url)
            kind = classify_link(url, base_url=page_url)
            files.append(
                {
                    "url": url,
                    "label": label,
                    "kind": kind,
                    "target_type": _document_target_type(label=label, url=url),
                }
            )
    return files


def parse_related_items(html: str, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen: set[str] = set()
    for anchor in root.iter("a"):
        if not _has_class(anchor, "related-rule-link"):
            continue
        url = absolute_url(page_url, anchor.get("href"))
        route = parse_detail_route(url)
        if url is None or route is None or route.item_id in seen:
            continue
        seen.add(route.item_id)
        row = _nearest_ancestor(anchor, "tr")
        cells = _direct_children(row, {"td", "th"}) if row is not None else []
        agency = _first_child_text(cells[0], "p") if cells else None
        snippet = clean_snippet(cells[1] if len(cells) > 1 else row, max_length=SNIPPET_LENGTH)
        item = LegalItem(
            id=route.item_id,
            title=_anchor_label(anchor, url=url),
            document_type=_document_type_from_route(route),
            url=route.url,
            snippet=snippet,
            facets={"relationship": "related", "type": route.type_slug, "year": route.year},
            source_fields=_compact(
                {
                    "route": route.to_dict(),
                    "path": route.path,
                    "agency": agency,
                    "relationship": "related",
                }
            ),
            raw={"row_text": row.text(), "href": anchor.get("href")} if include_raw and row is not None else {},
            provenance=_provenance(
                fetched_urls=[page_url],
                source_response_id=route.item_id,
                raw={"source_path": urlparse(page_url).path},
            ),
        )
        items.append(item)
    return items


def search_params(query: Mapping[str, Any], *, page: int = 1) -> JsonDict:
    params: JsonDict = {}
    if page > 1:
        params["page"] = str(page)
    _add_param(params, "q[terms][raw_type]", query.get("raw_type"))
    _add_param(params, "q[terms][number]", query.get("number"))
    _add_param(params, "q[terms][year]", query.get("year"))
    _add_param(params, "q[phrase]", query.get("text"))
    _add_param(params, "q[without_words]", query.get("without_words"))
    _add_param(params, "q[with_some_words]", query.get("some_words"))
    _add_param(params, "q[date_ranges][publication_date][gte]", query.get("published_from"))
    _add_param(params, "q[date_ranges][publication_date][lte]", query.get("published_to"))
    _add_param(params, "q[terms][bulletin_number]", query.get("bulletin_number"))
    _add_param(params, "q[sort]", query.get("sort"))
    return params


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("Normas PBA source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("search", handle_search, help="search Normas PBA", add_arguments=add_search_arguments)
    adapter.register_operation("get", handle_get, help="fetch a Normas PBA detail page", add_arguments=add_get_arguments)
    adapter.register_operation(
        "related",
        handle_related,
        help="list rules related to a Normas PBA detail page",
        add_arguments=add_related_arguments,
    )
    adapter.register_operation(
        "download",
        handle_download,
        help="download a Normas PBA detail PDF (original text/fundamentos)",
        add_arguments=add_download_arguments,
    )
    return adapter


def _card_to_item(
    card: HtmlNode,
    *,
    page_url: str,
    index: int,
    include_raw: bool,
) -> LegalItem | None:
    anchor = _rule_name_anchor(card)
    if anchor is None:
        return None
    url = absolute_url(page_url, anchor.get("href"))
    route = parse_detail_route(url)
    if route is None:
        return None
    fields = _field_pairs(card)
    normalized_fields = _normalized_fields(fields)
    publication_date = _optional_text(normalized_fields.get("publication_date"))
    summary = _first_node_text(card, "blockquote")
    title = _anchor_label(anchor, url=route.url)
    return LegalItem(
        id=route.item_id,
        title=title,
        date=publication_date,
        document_type=_document_type_from_route(route),
        url=route.url,
        snippet=clean_snippet(summary, max_length=SNIPPET_LENGTH),
        facets={
            key: value
            for key, value in {
                "type": route.type_slug,
                "year": route.year,
                "publication_date": publication_date,
            }.items()
            if value not in (None, "", [])
        },
        source_fields={
            "route": route.to_dict(),
            "path": route.path,
            "type": route.type_slug,
            "year": route.year,
            "number": route.number,
            "internal_id": route.internal_id,
            "fields": fields,
            **normalized_fields,
        },
        raw={"card_text": card.text(), "index": index} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=route.item_id,
            raw={"index": index, "href": route.path},
        ),
    )


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    norm_type = _optional_text(args.norm_type)
    raw_type = _optional_text(args.raw_type) or _raw_type(norm_type)
    sort = _sort_value(args.sort)
    query: JsonDict = {
        "text": _optional_text(args.text),
        "some_words": _optional_text(args.some_words),
        "without_words": _optional_text(args.without_words),
        "type": norm_type,
        "raw_type": raw_type,
        "number": _optional_text(args.number),
        "year": args.year,
        "published_from": _iso_date_arg(args.published_from, field="--from-date"),
        "published_to": _iso_date_arg(args.published_to, field="--to-date"),
        "bulletin_number": _optional_text(args.bulletin_number),
        "sort": sort,
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _next_page_state(
    *,
    search_page: NormasSearchPage,
    current_page: int,
    offset: int,
    limit: int,
    returned_count: int,
) -> tuple[int | None, int | None, bool]:
    next_offset = offset + limit
    if next_offset < len(search_page.items):
        return current_page, next_offset, True
    if search_page.has_next_page:
        return current_page + 1, 0, True
    if search_page.total is not None:
        consumed = (current_page - 1) * max(len(search_page.items), limit) + offset + returned_count
        return None, None, consumed < search_page.total and returned_count > 0
    return None, None, False


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _required_route(value: Any) -> DetailRoute:
    route = parse_detail_route(_optional_text(value))
    if route is None:
        raise usage_error("--path must be a Normas PBA detail path like /ar-b/ley/2018/15000/2459")
    return route


def parse_detail_route(value: str | None) -> DetailRoute | None:
    text = _optional_text(value)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc and parsed.netloc.lower() != "normas.gba.gob.ar":
        return None
    path = parsed.path if parsed.scheme or parsed.netloc else text.split("?", 1)[0].split("#", 1)[0]
    path = "/" + path.lstrip("/")
    match = _DETAIL_ROUTE_RE.match(path)
    if not match:
        return None
    return DetailRoute(
        path=path,
        type_slug=match.group("type").lower(),
        year=match.group("year"),
        number=match.group("number"),
        internal_id=match.group("internal_id"),
    )


def _field_pairs(root: HtmlNode) -> JsonDict:
    fields: JsonDict = {}
    for name_node in root.iter("span"):
        if not _has_class(name_node, "field-name"):
            continue
        label_text = _optional_text(name_node.text())
        if not label_text:
            continue
        info_node = _next_sibling_with_class(name_node, "field-info")
        value = _optional_text(info_node.text()) if info_node is not None else None
        label = label_text
        if value is None and ":" in label_text:
            label, value = label_text.split(":", 1)
            value = _optional_text(value)
        key = _field_key(label)
        if key and value is not None:
            fields[key] = value
    return fields


def _normalized_fields(fields: Mapping[str, str]) -> JsonDict:
    normalized: JsonDict = {}
    for key, value in fields.items():
        if key in {"publication_date", "promulgation_date"}:
            normalized[key] = normalize_date(value) or value
        else:
            normalized[key] = value
    return normalized


def _field_key(label: str) -> str:
    stripped = clean_text(label.rstrip(":")) or label
    return FIELD_KEY_BY_LABEL.get(_static_key(stripped), _static_key(stripped).replace(" ", "_"))


def _section_texts(rule: HtmlNode) -> JsonDict:
    sections: JsonDict = {}
    for section in _iter_by_class(rule, "rule-section"):
        current_key: str | None = None
        parts: list[str] = []
        for child in section.children:
            if not isinstance(child, HtmlNode):
                continue
            if child.tag == "h5" and _has_class(child, "section-title"):
                if current_key and parts:
                    sections[current_key] = clean_snippet(" ".join(parts), max_length=SNIPPET_LENGTH)
                current_key = _static_key(child.text()).replace(" ", "_")
                parts = []
                continue
            if current_key:
                text = child.text()
                if text:
                    parts.append(text)
        if current_key and parts:
            sections[current_key] = clean_snippet(" ".join(parts), max_length=SNIPPET_LENGTH)
    return sections


def _file_links(files: list[JsonDict]) -> list[JsonDict]:
    return [
        {
            "url": item["url"],
            "label": item["label"],
            "kind": item["kind"],
            "target_type": item["target_type"],
        }
        for item in files
    ]


def _related_item_link(item: LegalItem) -> JsonDict:
    return {
        "url": item.url,
        "label": item.title,
        "kind": "page",
        "target_type": "related_rule",
        "id": item.id,
    }


def _document_target_type(*, label: str, url: str) -> str:
    key = _static_key(label)
    kind = classify_link(url, base_url=BASE_URL)
    if "fundamento" in key:
        return "fundamentos"
    if "actualizado" in key:
        return "updated_text"
    if "original" in key:
        return "original_text"
    if kind == "pdf":
        return "pdf"
    return "document"


def _route_title(route: DetailRoute) -> str:
    return f"{_document_type_from_route(route).title()} {route.number}"


def _document_type_from_route(route: DetailRoute) -> str:
    return route.type_slug.replace("-", " ")


def _rule_name_anchor(card: HtmlNode) -> HtmlNode | None:
    for node in _iter_by_class(card, "rule-name"):
        for anchor in node.iter("a"):
            return anchor
    return None


def _anchor_label(anchor: HtmlNode, *, url: str) -> str:
    for value in (anchor.get("aria-label"), anchor.get("title"), anchor.text()):
        label = _optional_text(value)
        if label:
            return label
    return url.rsplit("/", 1)[-1] or url


def _first_node_text(root: HtmlNode, tag: str) -> str | None:
    node = root.find(tag)
    return node.text() if node is not None else None


def _first_id_text(root: HtmlNode, node_id: str) -> str | None:
    node = _node_by_id(root, node_id)
    return node.text() if node is not None else None


def _first_child_text(root: HtmlNode, tag: str) -> str | None:
    for child in root.children:
        if isinstance(child, HtmlNode) and child.tag == tag:
            return child.text()
    return None


def _node_by_id(root: HtmlNode, node_id: str) -> HtmlNode | None:
    for node in root.iter():
        if node.get("id") == node_id:
            return node
    return None


def _iter_by_class(root: HtmlNode, class_name: str) -> list[HtmlNode]:
    return [node for node in root.iter() if _has_class(node, class_name)]


def _has_class(node: HtmlNode, class_name: str) -> bool:
    classes = (node.get("class") or "").split()
    return class_name in classes


def _next_sibling_with_class(node: HtmlNode, class_name: str) -> HtmlNode | None:
    parent = node.parent
    if parent is None:
        return None
    try:
        index = parent.children.index(node)
    except ValueError:
        return None
    for child in parent.children[index + 1 :]:
        if isinstance(child, HtmlNode) and _has_class(child, class_name):
            return child
    return None


def _nearest_ancestor(node: HtmlNode, tag: str) -> HtmlNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _direct_children(node: HtmlNode | None, tags: set[str]) -> list[HtmlNode]:
    if node is None:
        return []
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _raw_type(value: str | None) -> str | None:
    if value is None:
        return None
    key = _static_key(value)
    raw_type = TYPE_RAW_BY_KEY.get(key)
    if raw_type is None:
        raise usage_error("unknown Normas PBA type", details={"type": value, "known_types": sorted(TYPE_RAW_BY_KEY)})
    return raw_type


def _sort_value(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text in SORT_VALUES:
        return text
    key = _static_key(text)
    sort = SORT_RAW_BY_KEY.get(key)
    if sort is None:
        raise usage_error("unknown Normas PBA sort", details={"sort": text, "known_sorts": sorted(SORT_VALUES)})
    return sort


def _iso_date_arg(value: Any, *, field: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise usage_error(f"{field} must be an ISO date YYYY-MM-DD")
    return text


def _year(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1800 or parsed > 2200:
        raise argparse.ArgumentTypeError("must be between 1800 and 2200")
    return parsed


def _page_number(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return parsed


def _parse_int(value: str | None) -> int | None:
    text = _optional_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _add_param(params: JsonDict, key: str, value: Any) -> None:
    if value not in (None, ""):
        params[key] = str(value)


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {str(key): item for key, item in value.items() if item not in (None, "", [])}


def _last_update(body: str) -> str | None:
    match = _LAST_UPDATE_RE.search(body)
    return match.group("value") if match else None


def _static_key(value: Any) -> str:
    text = clean_text(str(value)) if value is not None else None
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Referer": HOME_URL})


def _useful_headers(response: httpx.Response) -> JsonDict:
    return {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "etag", "last-modified"}
    }


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[HOME_URL, SEARCH_URL],
        fetched_urls=[_public_url(url) for url in fetched_urls],
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "normas.gba.gob.ar":
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, parsed.fragment))


register_adapter(build_adapter(), replace=True)
