"""Infoleg national norms search adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import httpx

from apps.legal.cache import SearchCacheRecord, load_search_state, save_search_state
from apps.legal.errors import not_found, parse_error, usage_error
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


SOURCE_ID = "infoleg"
SOURCE_MAP = "apps/legal/docs/infoleg_normas_nacionales.md"

BASE_URL = "https://servicios.infoleg.gob.ar"
HOME_URL = f"{BASE_URL}/infolegInternet/"
SEARCH_URL = f"{BASE_URL}/infolegInternet/buscarNormas.do"
DETAIL_URL = f"{BASE_URL}/infolegInternet/verNorma.do"
LINKS_URL = f"{BASE_URL}/infolegInternet/verVinculos.do"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 360

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DETAIL_HEADER_RE = re.compile(
    r"^(?P<type>.+?)\s+(?P<number>\d+(?:\s*/\s*\d{2,4})?)\s+(?P<agency>.+)$",
    re.IGNORECASE,
)
_PUBLICATION_RE = re.compile(
    r"Publicada\s+en\s+el\s+Bolet[ií]n\s+Oficial\s+del\s+(?P<date>\d{1,2}-[a-záéíóú]{3}-\d{4})",
    re.IGNORECASE,
)
_BULLETIN_NUMBER_RE = re.compile(r"N[uú]mero:\s*(?P<number>\d+)", re.IGNORECASE)
_BULLETIN_PAGE_RE = re.compile(r"P[aá]gina:\s*(?P<page>\S+)", re.IGNORECASE)

TEXT_MODES = {"original", "updated", "metadata"}
LINK_MODE_CODES: Mapping[str, str] = {"active": "1", "passive": "2"}
LINK_MODE_BY_CODE: Mapping[str, str] = {code: mode for mode, code in LINK_MODE_CODES.items()}


def _static_key(value: Any) -> str:
    text = clean_text(str(value)) if value is not None else None
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


TYPE_LABEL_BY_CODE: Mapping[str, str] = {
    "1": "Ley",
    "2": "Decreto",
    "3": "Resolución",
    "4": "Disposición",
    "5": "Circular",
    "6": "Comunicación",
    "7": "Decreto/Ley",
    "8": "Decisión Administrativa",
    "9": "Nota Externa",
    "10": "Instrucción",
    "11": "Acta",
    "12": "Acordada",
    "13": "Comunicado",
    "14": "Decisión",
    "15": "Directiva",
    "16": "Nota",
    "17": "Acuerdo",
    "18": "Memorándum",
    "19": "Protocolo",
    "20": "Convenio",
    "21": "Misión",
    "22": "Recomendación",
    "23": "Interpretación",
    "24": "Laudo",
    "27": "Actuación",
    "28": "Providencia",
    "29": "Ordenanza",
}
TYPE_CODE_BY_KEY: Mapping[str, str] = {
    _static_key(label): code for code, label in TYPE_LABEL_BY_CODE.items()
} | {
    "decision administrativa": "8",
    "decreto ley": "7",
    "memorandum": "18",
}

_TOTAL_RE = re.compile(
    r"Cantidad de Normas Encontradas:\s*(?P<total>[\d.]+)\s+en\s+(?P<pages>[\d.]+)",
    re.IGNORECASE,
)
_CURRENT_PAGE_RE = re.compile(
    r"<a[^>]*font-weight\s*:\s*700[^>]*>\s*<i>\s*(?P<page>\d+)\s*</i>",
    re.IGNORECASE,
)
_NORM_LABEL_RE = re.compile(
    r"^(?P<kind>[^\d]+?)\s*(?P<number>\d+(?:\s*/\s*\d{2,4})?)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InfolegHome:
    url: str
    html: str
    form_action: str
    headers: JsonDict


@dataclass(frozen=True)
class InfolegSearchPage:
    url: str
    html: str
    form_action: str
    hidden_fields: JsonDict
    items: list[LegalItem]
    total: int | None
    total_pages: int | None
    current_page: int | None
    headers: JsonDict


@dataclass(frozen=True)
class InfolegDetail:
    infoleg_id: str
    url: str
    html: str
    headers: JsonDict


@dataclass(frozen=True)
class InfolegTextPage:
    infoleg_id: str
    text_mode: str
    url: str
    html: str
    body: str
    headers: JsonDict


@dataclass(frozen=True)
class InfolegLinksPage:
    infoleg_id: str
    mode: str
    url: str
    html: str
    items: list[LegalItem]
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--type", dest="norm_type", help="Infoleg norm type, e.g. ley or resolucion")
    parser.add_argument("--number", help="norm number without punctuation")
    parser.add_argument("--year", type=_year, help="sanction year; omitted for law searches")
    parser.add_argument("--text", "--q", dest="text", help="free text search")
    parser.add_argument("--agency", help="raw Infoleg dependencia id")
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
    parser.add_argument("--page", type=_page_number, help="1-based Infoleg result page to fetch")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="infoleg_id", help="canonical Infoleg norm id")
    parser.add_argument(
        "--text",
        choices=sorted(TEXT_MODES),
        default="metadata",
        help="fetch original text, updated text, or only detail metadata",
    )


def add_links_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="infoleg_id", help="canonical Infoleg norm id")
    parser.add_argument(
        "--mode",
        choices=sorted(LINK_MODE_CODES),
        default="active",
        help="active outgoing links (default) or passive incoming links",
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    requested_page = int(getattr(args, "page", None) or cursor_payload.get("page") or 1)

    with _make_client() as client:
        home = fetch_home(client=client)
        form_data, warnings = search_form_data(query)
        search_page = fetch_search_page(
            action_url=home.form_action,
            form_data=form_data,
            referer=home.url,
            client=client,
            include_raw=bool(args.raw),
        )
        fetched_urls = [home.url, search_page.url]
        if requested_page > 1:
            search_page = fetch_search_page(
                action_url=search_page.form_action,
                form_data=pagination_form_data(form_data, search_page.hidden_fields, page=requested_page),
                referer=search_page.url,
                client=client,
                include_raw=bool(args.raw),
            )
            fetched_urls.append(search_page.url)
        items = search_page.items[:limit]
        has_more = _has_more(
            total=search_page.total,
            total_pages=search_page.total_pages,
            current_page=search_page.current_page,
            available_count=len(search_page.items),
            returned_count=len(items),
        )
        search_id = (
            _save_search_state(
                client=client,
                query=query,
                limit=limit,
                home=home,
                form_data=form_data,
                search_page=search_page,
                returned_items=items,
                fetched_urls=fetched_urls,
            )
            if has_more
            else None
        )

    response_query: JsonDict = {**query, "limit": limit}
    if requested_page != 1 or getattr(args, "page", None) is not None:
        response_query["page"] = requested_page

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query=response_query,
        items=items,
        page=PageInfo(
            limit=limit,
            offset=0,
            page=search_page.current_page,
            total=search_page.total,
            has_more=has_more,
            search_id=search_id,
        ),
        provenance=_provenance(
            fetched_urls=fetched_urls,
            raw={
                "home_headers": home.headers,
                "result_headers": search_page.headers,
                "home_form_action": _public_url(home.form_action),
                "result_form_action": _public_url(search_page.form_action),
                "current_page": search_page.current_page,
                "total_pages": search_page.total_pages,
                "search_id": search_id,
                "requested_page": requested_page,
            },
        ),
        facets={"type_codes": dict(TYPE_LABEL_BY_CODE)},
        warnings=warnings,
    )


def handle_next(args: argparse.Namespace) -> LegalResponse:
    search_id = _required_search_id(args.search_id)
    record = load_search_state(search_id)
    if record is None:
        raise not_found(
            "Infoleg search state was not found or expired",
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

    form_data = _mapping_from_state(state, "form_data")
    action_url = _text_from_state(state, "result_form_action") or SEARCH_URL
    referer = _text_from_state(state, "result_url") or HOME_URL

    with _make_client() as client:
        restore_cookies(client=client, cookies=record.cookies)
        search_page = fetch_search_page(
            action_url=action_url,
            form_data=pagination_form_data(form_data, record.hidden_fields, next_page=True),
            referer=referer,
            client=client,
            include_raw=bool(args.raw),
        )
        items = search_page.items[:limit]
        has_more = _has_more(
            total=search_page.total,
            total_pages=search_page.total_pages,
            current_page=search_page.current_page,
            available_count=len(search_page.items),
            returned_count=len(items),
        )
        _save_search_state(
            client=client,
            query=query,
            limit=limit,
            form_data=form_data,
            search_page=search_page,
            returned_items=items,
            search_id=record.search_id,
            home_url=_text_from_state(state, "home_url") or HOME_URL,
            home_form_action=_text_from_state(state, "home_form_action") or SEARCH_URL,
            fetched_urls=[referer, search_page.url],
        )

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=0,
            page=search_page.current_page,
            total=search_page.total,
            has_more=has_more,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=[referer, search_page.url],
            raw={
                "result_headers": search_page.headers,
                "result_form_action": _public_url(search_page.form_action),
                "current_page": search_page.current_page,
                "total_pages": search_page.total_pages,
                "search_id": record.search_id,
            },
        ),
        facets={"type_codes": dict(TYPE_LABEL_BY_CODE)},
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    infoleg_id = _required_infoleg_id(args.infoleg_id)
    text_mode = _text_mode(args.text)

    with _make_client() as client:
        detail = fetch_detail(infoleg_id, client=client)
        text_page = (
            fetch_text_page(detail=detail, text_mode=text_mode, client=client)
            if text_mode in {"original", "updated"}
            else None
        )

    document = detail_to_document(
        detail,
        text_mode=text_mode,
        text_page=text_page,
        include_raw=bool(args.raw),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={"id": infoleg_id, "text": text_mode},
        document=document,
        provenance=document.provenance,
    )


def handle_links(args: argparse.Namespace) -> LegalResponse:
    infoleg_id = _required_infoleg_id(args.infoleg_id)
    mode = _link_mode(args.mode)
    limit = int(args.limit) if args.limit else None

    with _make_client() as client:
        page = fetch_links_page(infoleg_id=infoleg_id, mode=mode, client=client, include_raw=bool(args.raw))

    items = page.items[:limit] if limit is not None else page.items
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="links",
        query={"id": infoleg_id, "mode": mode, **({"limit": limit} if limit is not None else {})},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=0,
            page=1,
            total=len(page.items),
            has_more=limit is not None and len(items) < len(page.items),
        ),
        provenance=_provenance(
            fetched_urls=[page.url],
            source_response_id=f"{infoleg_id}:{mode}",
            raw={"headers": page.headers, "mode_code": LINK_MODE_CODES[mode]},
        ),
    )


def fetch_home(*, client: LegalHttpClient | None = None) -> InfolegHome:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", HOME_URL)
        form_action = search_form_action(response.text, page_url=str(response.url))
        return InfolegHome(
            url=str(response.url),
            html=response.text,
            form_action=form_action,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def fetch_search_page(
    *,
    action_url: str,
    form_data: Mapping[str, Any],
    referer: str,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> InfolegSearchPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request(
            "POST",
            action_url,
            data=dict(form_data),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": referer,
            },
        )
        return parse_search_response(response, include_raw=include_raw)
    finally:
        if owns_client:
            http.close()


def fetch_detail(
    infoleg_id: str,
    *,
    client: LegalHttpClient | None = None,
) -> InfolegDetail:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", DETAIL_URL, params={"id": infoleg_id})
        html = _html_response_text(response)
        if not html.strip():
            raise not_found(
                "Infoleg detail page was empty",
                details={"id": infoleg_id},
                provenance=_provenance(fetched_urls=[str(response.url)], source_response_id=infoleg_id),
            )
        return InfolegDetail(
            infoleg_id=infoleg_id,
            url=str(response.url),
            html=html,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def fetch_text_page(
    *,
    detail: InfolegDetail,
    text_mode: str,
    client: LegalHttpClient | None = None,
) -> InfolegTextPage:
    mode = _text_mode(text_mode)
    if mode == "metadata":
        raise usage_error("metadata mode does not fetch an Infoleg text page")
    text_url = _detail_text_url(detail, text_mode=mode)

    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", text_url, headers={"Referer": detail.url})
        html = _html_response_text(response)
        body = _text_page_body(html)
        if not body:
            raise parse_error(
                "Infoleg text page did not contain visible text",
                details={"id": detail.infoleg_id, "text": mode, "url": _public_url(str(response.url))},
                provenance=_provenance(fetched_urls=[detail.url, str(response.url)], source_response_id=detail.infoleg_id),
            )
        return InfolegTextPage(
            infoleg_id=detail.infoleg_id,
            text_mode=mode,
            url=str(response.url),
            html=html,
            body=body,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def fetch_links_page(
    *,
    infoleg_id: str,
    mode: str,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> InfolegLinksPage:
    normalized_mode = _link_mode(mode)
    mode_code = LINK_MODE_CODES[normalized_mode]
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", LINKS_URL, params={"modo": mode_code, "id": infoleg_id})
        html = _html_response_text(response)
        items = parse_relationship_items(
            html,
            page_url=str(response.url),
            source_infoleg_id=infoleg_id,
            mode=normalized_mode,
            include_raw=include_raw,
        )
        return InfolegLinksPage(
            infoleg_id=infoleg_id,
            mode=normalized_mode,
            url=str(response.url),
            html=html,
            items=items,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def parse_search_response(response: httpx.Response, *, include_raw: bool = False) -> InfolegSearchPage:
    html = _html_response_text(response)
    page_url = str(response.url)
    form_action = search_form_action(html, page_url=page_url, required=False) or SEARCH_URL
    total, total_pages = parse_result_counts(html)
    current_page = parse_current_page(html) or (1 if total is not None else None)
    items = parse_search_items(html, page_url=page_url, include_raw=include_raw)
    if total is None and items:
        total = len(items)
    return InfolegSearchPage(
        url=page_url,
        html=html,
        form_action=form_action,
        hidden_fields=parse_hidden_fields(html),
        items=items,
        total=total,
        total_pages=total_pages,
        current_page=current_page,
        headers=_useful_headers(response),
    )


def detail_to_document(
    detail: InfolegDetail,
    *,
    text_mode: str = "metadata",
    text_page: InfolegTextPage | None = None,
    include_raw: bool = False,
) -> LegalDocument:
    mode = _text_mode(text_mode)
    root = parse_html(detail.html)
    visible_text = text_content(root) or ""
    if not visible_text:
        raise parse_error(
            "Infoleg detail page did not contain visible text",
            details={"id": detail.infoleg_id},
            provenance=_provenance(fetched_urls=[detail.url], source_response_id=detail.infoleg_id),
        )

    links = detail_links(detail.html, page_url=detail.url)
    metadata = detail_metadata(root=root, body=visible_text, infoleg_id=detail.infoleg_id, links=links)
    fetched_urls = [detail.url]
    body = None
    document_url = _public_url(detail.url)
    text_headers: JsonDict | None = None
    if text_page is not None:
        body = text_page.body
        document_url = _public_url(text_page.url)
        fetched_urls.append(text_page.url)
        text_headers = text_page.headers

    source_fields: JsonDict = {
        "infoleg_id": detail.infoleg_id,
        "text_mode": mode,
        "detail_url": _public_url(detail.url),
        "original_text_url": metadata.get("original_text_url"),
        "updated_text_url": metadata.get("updated_text_url"),
        "active_links_url": metadata.get("active_links_url"),
        "passive_links_url": metadata.get("passive_links_url"),
    }
    provenance_raw: JsonDict = {"detail_headers": detail.headers}
    if text_headers is not None:
        provenance_raw["text_headers"] = text_headers

    return LegalDocument(
        id=f"{SOURCE_ID}:{detail.infoleg_id}",
        title=_optional_text(metadata.get("title")) or detail.infoleg_id,
        date=_optional_text(metadata.get("publication_date")) or _optional_text(metadata.get("norm_date")),
        document_type=_optional_text(metadata.get("type")),
        body=body,
        url=document_url,
        content_type="text/html",
        text_format="plain_text" if body else None,
        metadata=metadata,
        links=links,
        source_fields={key: value for key, value in source_fields.items() if value not in (None, "", [])},
        raw={
            "detail_html": detail.html,
            "text_html": text_page.html if text_page is not None else None,
            "headers": provenance_raw,
        }
        if include_raw
        else {},
        provenance=_provenance(
            fetched_urls=fetched_urls,
            source_response_id=detail.infoleg_id,
            raw=provenance_raw,
        ),
    )


def detail_metadata(
    *,
    root: HtmlNode,
    body: str,
    infoleg_id: str,
    links: list[JsonDict],
) -> JsonDict:
    header = _first_node_text(root, "strong")
    header_parts = _parse_detail_header(header)
    subject = _first_class_text(root, "destacado")
    heading = _first_node_text(root, "h1")
    title = _detail_title(header_parts=header_parts, header=header, heading=heading)
    publication_date = _publication_date(body)
    metadata: JsonDict = {
        "infoleg_id": infoleg_id,
        "id": infoleg_id,
        "title": title,
        "header": header,
        "type": header_parts.get("type"),
        "number": header_parts.get("number"),
        "agency": header_parts.get("agency"),
        "subject": subject,
        "heading": heading,
        "norm_date": _norm_date(root, body),
        "publication_date": publication_date,
        "bulletin_number": _regex_group(_BULLETIN_NUMBER_RE, body, "number"),
        "bulletin_page": _regex_group(_BULLETIN_PAGE_RE, body, "page"),
        "summary": _labeled_section(root, "Resumen"),
        "observations": _labeled_section(root, "Observaciones"),
        "original_text_url": _first_detail_link_url(links, "original_text"),
        "updated_text_url": _first_detail_link_url(links, "updated_text"),
        "active_links_url": _first_detail_link_url(links, "relationships_active"),
        "passive_links_url": _first_detail_link_url(links, "relationships_passive"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [])}


def detail_links(html: str, *, page_url: str) -> list[JsonDict]:
    links: list[JsonDict] = []
    for link in extract_links(html, base_url=page_url):
        url = _optional_text(link.get("url"))
        if url is None:
            continue
        label = _optional_text(link.get("label")) or url
        target_type = _detail_link_type(label=label, url=url)
        if target_type is None:
            continue
        public_url = _public_url(url)
        normalized: JsonDict = {
            "url": public_url,
            "label": label,
            "kind": classify_link(public_url, base_url=page_url),
            "target_type": target_type,
        }
        mode = _relationship_mode_from_url(public_url)
        if mode:
            normalized["relationship_mode"] = mode
        links.append(normalized)
    return _dedupe_links(links)


def parse_relationship_items(
    html: str,
    *,
    page_url: str,
    source_infoleg_id: str,
    mode: str,
    include_raw: bool = False,
) -> list[LegalItem]:
    normalized_mode = _link_mode(mode)
    root = parse_html(html)
    items: list[LegalItem] = []
    seen_ids: set[str] = set()
    for row in root.iter("tr"):
        cells = _direct_children(row, {"td", "th"})
        if len(cells) < 3:
            continue
        norm_link = _primary_norm_link(cells[0], page_url=page_url)
        if norm_link is None:
            continue
        infoleg_id = str(norm_link["infoleg_id"])
        if infoleg_id in seen_ids:
            continue
        seen_ids.add(infoleg_id)
        items.append(
            _relationship_row_to_item(
                cells,
                norm_link=norm_link,
                page_url=page_url,
                source_infoleg_id=source_infoleg_id,
                mode=normalized_mode,
                include_raw=include_raw,
            )
        )
    return items


def search_form_action(html: str, *, page_url: str, required: bool = True) -> str | None:
    root = parse_html(html)
    for form in root.iter("form"):
        action = clean_text(form.get("action"))
        form_name = clean_text(form.get("name") or form.get("id"))
        if action and ("buscarNormas.do" in action or form_name == "busquedaNormasForm"):
            resolved = absolute_url(page_url, action)
            if resolved:
                return resolved
    if required:
        raise parse_error(
            "Infoleg search form action was not found",
            provenance=_provenance(fetched_urls=[page_url]),
        )
    return None


def search_form_data(query: Mapping[str, Any]) -> tuple[JsonDict, list[str]]:
    data: JsonDict = {}
    warnings: list[str] = []
    type_code = _optional_text(query.get("type_code"))
    if type_code:
        data["tipoNorma"] = type_code

    for query_key, form_key in (
        ("number", "numero"),
        ("text", "texto"),
        ("agency", "dependencia"),
    ):
        value = _optional_text(query.get(query_key))
        if value is not None:
            data[form_key] = value

    year = query.get("year")
    if year is not None:
        if type_code == "1":
            warnings.append("year was not submitted because Infoleg rejects anioSancion for Ley searches")
        else:
            data["anioSancion"] = str(year)

    data.update(_publication_date_fields(query.get("published_from"), prefix="Desde"))
    data.update(_publication_date_fields(query.get("published_to"), prefix="Hasta"))
    return data, warnings


def pagination_form_data(
    form_data: Mapping[str, Any],
    hidden_fields: Mapping[str, Any],
    *,
    page: int | None = None,
    next_page: bool = False,
) -> JsonDict:
    if next_page == (page is not None):
        raise ValueError("pagination form data requires exactly one pagination target")
    data: JsonDict = {str(key): value for key, value in form_data.items()}
    data.update({str(key): value for key, value in hidden_fields.items()})
    if next_page:
        data["desplazamiento"] = "+"
        data["irAPagina"] = ""
    else:
        data["desplazamiento"] = "AP"
        data["irAPagina"] = str(page)
    return data


def parse_result_counts(html: str) -> tuple[int | None, int | None]:
    text = text_content(html) or ""
    match = _TOTAL_RE.search(text)
    if not match:
        return None, None
    return _digits_to_int(match.group("total")), _digits_to_int(match.group("pages"))


def parse_current_page(html: str) -> int | None:
    match = _CURRENT_PAGE_RE.search(html)
    if not match:
        return None
    return int(match.group("page"))


def parse_hidden_fields(html: str) -> JsonDict:
    fields: JsonDict = {}
    root = parse_html(html)
    for node in root.iter("input"):
        input_type = _key(node.get("type"))
        name = clean_text(node.get("name"))
        if input_type == "hidden" and name:
            fields[name] = clean_text(node.get("value")) or ""
    return fields


def parse_search_items(html: str, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen_ids: set[str] = set()
    for row in root.iter("tr"):
        cells = _direct_children(row, {"td", "th"})
        if len(cells) < 3:
            continue
        norm_link = _primary_norm_link(cells[0], page_url=page_url)
        if norm_link is None:
            continue
        infoleg_id = norm_link["infoleg_id"]
        if infoleg_id in seen_ids:
            continue
        seen_ids.add(infoleg_id)
        items.append(
            _row_to_item(
                cells,
                norm_link=norm_link,
                page_url=page_url,
                include_raw=include_raw,
            )
        )
    return items


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("Infoleg source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation(
        "search",
        handle_search,
        help="search Infoleg national norms",
        add_arguments=add_search_arguments,
    )
    adapter.register_operation(
        "get",
        handle_get,
        help="fetch Infoleg detail metadata and original or updated text",
        add_arguments=add_get_arguments,
    )
    adapter.register_operation(
        "links",
        handle_links,
        help="fetch active or passive Infoleg relationship links",
        add_arguments=add_links_arguments,
    )
    adapter.register_operation(
        "next",
        handle_next,
        help="continue a cached Infoleg search",
    )
    return adapter


def _row_to_item(
    cells: list[HtmlNode],
    *,
    norm_link: JsonDict,
    page_url: str,
    include_raw: bool,
) -> LegalItem:
    label = _optional_text(norm_link.get("label")) or f"Infoleg {norm_link['infoleg_id']}"
    label_parts = _parse_norm_label(label)
    lines = _node_lines(cells[0])
    agency = _agency_from_lines(lines, label=label)
    date_text = _first_anchor_text(cells[1]) or cells[1].text()
    publication_date = normalize_date(date_text.replace("-", " ") if date_text else None)
    bulletin_url = _first_anchor_url(cells[1], page_url=page_url)
    description = _description_parts(cells[2])
    infoleg_id = str(norm_link["infoleg_id"])
    source_fields: JsonDict = {
        "infoleg_id": infoleg_id,
        "type": label_parts.get("type"),
        "number": label_parts.get("number"),
        "agency": agency,
        "publication_date_raw": clean_text(date_text),
        "bulletin_url": bulletin_url,
        "heading": description.get("heading"),
        "description": description.get("description"),
        "summary": description.get("summary"),
    }
    return LegalItem(
        id=f"{SOURCE_ID}:{infoleg_id}",
        title=label,
        date=publication_date,
        document_type=_optional_text(label_parts.get("type")),
        url=str(norm_link["url"]),
        snippet=clean_snippet(description.get("snippet"), max_length=SNIPPET_LENGTH),
        facets={
            key: value
            for key, value in {
                "type": label_parts.get("type"),
                "agency": agency,
                "publication_year": publication_date[:4] if publication_date else None,
            }.items()
            if value is not None
        },
        source_fields={key: value for key, value in source_fields.items() if value not in (None, "", [])},
        raw={
            "row": [cell.text() for cell in cells],
            "href": norm_link.get("href"),
        }
        if include_raw
        else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=infoleg_id,
            raw={"href": norm_link.get("href")},
        ),
    )


def _relationship_row_to_item(
    cells: list[HtmlNode],
    *,
    norm_link: JsonDict,
    page_url: str,
    source_infoleg_id: str,
    mode: str,
    include_raw: bool,
) -> LegalItem:
    label = _optional_text(norm_link.get("label")) or f"Infoleg {norm_link['infoleg_id']}"
    label_parts = _parse_norm_label(label)
    lines = _node_lines(cells[0])
    agency = _agency_from_lines(lines, label=label)
    date_text = cells[1].text()
    publication_date = normalize_date(date_text.replace("-", " ") if date_text else None)
    description = _description_parts(cells[2])
    infoleg_id = str(norm_link["infoleg_id"])
    public_url = _public_url(str(norm_link["url"]))
    source_fields: JsonDict = {
        "infoleg_id": infoleg_id,
        "source_infoleg_id": source_infoleg_id,
        "relationship_mode": mode,
        "relationship_mode_code": LINK_MODE_CODES[mode],
        "type": label_parts.get("type"),
        "number": label_parts.get("number"),
        "agency": agency,
        "publication_date_raw": clean_text(date_text),
        "heading": description.get("heading"),
        "description": description.get("description"),
        "summary": description.get("summary"),
    }
    return LegalItem(
        id=f"{SOURCE_ID}:{infoleg_id}",
        title=label,
        date=publication_date,
        document_type=_optional_text(label_parts.get("type")),
        url=public_url,
        snippet=clean_snippet(description.get("snippet"), max_length=SNIPPET_LENGTH),
        facets={
            key: value
            for key, value in {
                "type": label_parts.get("type"),
                "agency": agency,
                "publication_year": publication_date[:4] if publication_date else None,
                "relationship_mode": mode,
            }.items()
            if value is not None
        },
        source_fields={key: value for key, value in source_fields.items() if value not in (None, "", [])},
        raw={
            "row": [cell.text() for cell in cells],
            "href": _public_url(str(norm_link.get("href") or public_url)),
        }
        if include_raw
        else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=f"{source_infoleg_id}:{mode}:{infoleg_id}",
            raw={"href": _public_url(str(norm_link.get("href") or public_url))},
        ),
    )


def _required_infoleg_id(value: Any) -> str:
    text = _optional_text(value)
    if text and text.startswith(f"{SOURCE_ID}:"):
        text = text.split(":", 1)[1]
    if not text or not text.isdigit():
        raise usage_error("Infoleg --id must be numeric", details={"id": value})
    return text


def _text_mode(value: Any) -> str:
    mode = _key(value)
    if mode not in TEXT_MODES:
        raise usage_error("unknown Infoleg text mode", details={"text": value, "known_modes": sorted(TEXT_MODES)})
    return mode


def _link_mode(value: Any) -> str:
    mode = _key(value)
    if mode not in LINK_MODE_CODES:
        raise usage_error("unknown Infoleg link mode", details={"mode": value, "known_modes": sorted(LINK_MODE_CODES)})
    return mode


def _detail_text_url(detail: InfolegDetail, *, text_mode: str) -> str:
    target_type = "original_text" if text_mode == "original" else "updated_text"
    for link in detail_links(detail.html, page_url=detail.url):
        if link.get("target_type") == target_type:
            url = _optional_text(link.get("url"))
            if url:
                return url
    fallback = _fallback_text_url(detail.infoleg_id, text_mode=text_mode)
    if fallback is not None:
        return fallback
    raise parse_error(
        "Infoleg detail page did not expose the requested text URL",
        details={"id": detail.infoleg_id, "text": text_mode},
        provenance=_provenance(fetched_urls=[detail.url], source_response_id=detail.infoleg_id),
    )


def _fallback_text_url(infoleg_id: str, *, text_mode: str) -> str | None:
    try:
        numeric_id = int(infoleg_id)
    except ValueError:
        return None
    range_start = (numeric_id // 5000) * 5000
    range_end = range_start + 4999
    filename = "norma.htm" if text_mode == "original" else "texact.htm"
    return f"{BASE_URL}/infolegInternet/anexos/{range_start}-{range_end}/{infoleg_id}/{filename}"


def _text_page_body(html: str) -> str:
    return text_content(html) or ""


def _html_response_text(response: httpx.Response) -> str:
    text = response.text
    content_type = response.headers.get("content-type", "").lower()
    if "iso-8859-1" in content_type:
        return response.content.decode("iso-8859-1", errors="replace")
    if "\ufffd" not in text:
        return text
    latin1 = response.content.decode("iso-8859-1", errors="replace")
    return latin1 if latin1.count("\ufffd") <= text.count("\ufffd") else text


def _parse_detail_header(header: str | None) -> JsonDict:
    text = clean_text(header)
    if not text:
        return {}
    match = _DETAIL_HEADER_RE.match(text)
    if not match:
        return _parse_norm_label(text)
    number = clean_text((match.group("number") or "").replace(" ", ""))
    return {
        "type": clean_text(match.group("type")),
        "number": number,
        "agency": clean_text(match.group("agency")),
    }


def _detail_title(*, header_parts: Mapping[str, Any], header: str | None, heading: str | None) -> str:
    norm_label = " ".join(
        part
        for part in (
            _optional_text(header_parts.get("type")),
            _optional_text(header_parts.get("number")),
        )
        if part
    )
    if norm_label and heading:
        return f"{norm_label} - {heading}"
    return heading or norm_label or clean_text(header) or ""


def _norm_date(root: HtmlNode, body: str) -> str | None:
    for span in root.iter("span"):
        classes = span.get("class") or ""
        if "vr_azul11" in classes:
            text = span.text()
            normalized = normalize_date(text.replace("-", " ") if text else None)
            if normalized:
                return normalized
    return normalize_date(body)


def _publication_date(body: str) -> str | None:
    match = _PUBLICATION_RE.search(body)
    if not match:
        return None
    return normalize_date(match.group("date").replace("-", " "))


def _regex_group(pattern: re.Pattern[str], value: str, group: str) -> str | None:
    match = pattern.search(value)
    return clean_text(match.group(group)) if match else None


def _labeled_section(root: HtmlNode, label: str) -> str | None:
    label_key = _key(label)
    for paragraph in root.iter("p"):
        lines = _node_lines(paragraph)
        if not lines:
            continue
        first = lines[0]
        first_key = _key(first)
        if first_key == label_key:
            return clean_text(" ".join(lines[1:])) or None
        prefix = f"{label_key} "
        if first_key.startswith(prefix):
            without_label = re.sub(rf"^\s*{re.escape(label)}\s*:?\s*", "", first, flags=re.IGNORECASE)
            remainder = " ".join([without_label, *lines[1:]])
            return clean_text(remainder) or None
    return None


def _detail_link_type(*, label: str, url: str) -> str | None:
    search_key = _key(f"{label} {url}")
    if "norma htm" in search_key or "texto completo" in search_key:
        return "original_text"
    if "texact htm" in search_key or "texto actualizado" in search_key:
        return "updated_text"
    if "vervinculos" in search_key:
        mode = _relationship_mode_from_url(url)
        return f"relationships_{mode}" if mode else "relationships"
    if "page id 216" in search_key or "boletin oficial" in search_key:
        return "official_gazette"
    return None


def _relationship_mode_from_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("modo")
    if not values:
        return None
    return LINK_MODE_BY_CODE.get(clean_text(values[0]) or "")


def _first_detail_link_url(links: list[JsonDict], target_type: str) -> str | None:
    for link in links:
        if link.get("target_type") == target_type:
            return _optional_text(link.get("url"))
    return None


def _dedupe_links(links: list[JsonDict]) -> list[JsonDict]:
    output: list[JsonDict] = []
    seen: set[str] = set()
    for link in links:
        url = _optional_text(link.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        output.append({key: value for key, value in link.items() if value not in (None, "", [])})
    return output


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    type_input = _optional_text(args.norm_type)
    type_code = normalize_type_code(type_input)
    query: JsonDict = {
        "type": type_input,
        "type_code": type_code,
        "type_label": TYPE_LABEL_BY_CODE.get(type_code) if type_code else None,
        "number": _optional_text(args.number),
        "year": args.year,
        "text": _optional_text(args.text),
        "agency": _optional_text(args.agency),
        "published_from": _iso_date_string(args.published_from, flag="--from-date"),
        "published_to": _iso_date_string(args.published_to, flag="--to-date"),
    }
    normalized = {key: value for key, value in query.items() if value not in (None, "")}
    if not any(
        key in normalized
        for key in ("type_code", "number", "year", "text", "agency", "published_from", "published_to")
    ):
        raise usage_error("at least one Infoleg search filter is required")
    return normalized


def _query_from_record(record: SearchCacheRecord) -> JsonDict:
    query = record.cursor_payload.get("query")
    if not isinstance(query, Mapping):
        raise parse_error(
            "cached Infoleg search state is missing query metadata",
            details={"search_id": record.search_id},
        )
    return {str(key): value for key, value in query.items() if value not in (None, "")}


def _cached_page_items(record: SearchCacheRecord) -> list[JsonDict]:
    value = record.cursor_payload.get("available_items")
    if value is None:
        return []
    if not isinstance(value, list):
        raise parse_error(
            "cached Infoleg search state has malformed available items",
            details={"search_id": record.search_id},
        )
    items: list[JsonDict] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise parse_error(
                "cached Infoleg search state has malformed available items",
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
    current_page = _int_from_state(state, "current_page")
    total_pages = _int_from_state(state, "total_pages")
    total = _int_from_state(state, "total")
    has_more = new_returned_count < len(_cached_page_items(record))
    if not has_more and total_pages is not None and current_page is not None:
        has_more = current_page < total_pages

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=new_returned_count - len(items),
            page=current_page,
            total=total,
            has_more=has_more,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=_cached_fetched_urls(record),
            raw={
                "from_cache": True,
                "current_page": current_page,
                "total_pages": total_pages,
                "search_id": record.search_id,
            },
        ),
        facets={"type_codes": dict(TYPE_LABEL_BY_CODE)},
    )


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    from apps.legal.pagination import decode_cursor

    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _validate_search_record(record: SearchCacheRecord) -> None:
    if record.source != SOURCE_ID:
        raise usage_error(
            "search id belongs to a different source",
            details={"search_id": record.search_id, "source": record.source},
        )
    if not isinstance(record.cursor_payload, Mapping):
        raise parse_error(
            "cached Infoleg search state is malformed",
            details={"search_id": record.search_id},
        )


def _required_search_id(value: Any) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error("Infoleg next requires --search-id")
    return text


def _mapping_from_state(state: Mapping[str, Any], key: str) -> JsonDict:
    value = state.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise parse_error("cached Infoleg search state is malformed", details={"field": key})
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _text_from_state(state: Mapping[str, Any], key: str) -> str | None:
    return _optional_text(state.get(key))


def _int_from_state(state: Mapping[str, Any], key: str) -> int | None:
    value = state.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise parse_error("cached Infoleg search state is malformed", details={"field": key})
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError as exc:
        raise parse_error("cached Infoleg search state is malformed", details={"field": key}) from exc


def _cached_fetched_urls(record: SearchCacheRecord) -> list[str]:
    value = record.raw_provenance.get("fetched_urls")
    if not isinstance(value, list):
        result_url = _text_from_state(record.cursor_payload, "result_url")
        return [result_url] if result_url else [SEARCH_URL]
    urls = [url for url in (_optional_text(item) for item in value) if url]
    return urls or [SEARCH_URL]


def normalize_type_code(value: str | None) -> str | None:
    if value is None:
        return None
    if value.isdigit():
        if value in TYPE_LABEL_BY_CODE:
            return value
        raise usage_error("unknown Infoleg type code", details={"type": value})
    code = TYPE_CODE_BY_KEY.get(_key(value))
    if code is None:
        raise usage_error("unknown Infoleg type", details={"type": value, "known_types": sorted(TYPE_CODE_BY_KEY)})
    return code


def _save_search_state(
    *,
    client: LegalHttpClient,
    query: Mapping[str, Any],
    limit: int,
    home: InfolegHome | None = None,
    home_url: str | None = None,
    home_form_action: str | None = None,
    form_data: Mapping[str, Any],
    search_page: InfolegSearchPage,
    returned_items: list[LegalItem],
    search_id: str | None = None,
    fetched_urls: list[str] | None = None,
) -> str:
    resolved_home_url = home.url if home is not None else home_url or HOME_URL
    resolved_home_form_action = home.form_action if home is not None else home_form_action or SEARCH_URL
    resolved_fetched_urls = fetched_urls or [resolved_home_url, search_page.url]
    record = save_search_state(
        source=SOURCE_ID,
        query={**query, "limit": limit},
        search_id=search_id,
        cookies=client.cookies.jar,
        store_cookies=True,
        hidden_fields=search_page.hidden_fields,
        cursor_payload={
            "query": dict(query),
            "limit": limit,
            "form_data": dict(form_data),
            "home_url": resolved_home_url,
            "home_form_action": resolved_home_form_action,
            "result_url": search_page.url,
            "result_form_action": search_page.form_action,
            "current_page": search_page.current_page,
            "total": search_page.total,
            "total_pages": search_page.total_pages,
            "available_count": len(search_page.items),
            "available_items": [item.to_dict() for item in search_page.items],
            "returned_count": len(returned_items),
        },
        raw_provenance={
            "source_map": SOURCE_MAP,
            "fetched_urls": resolved_fetched_urls,
            "headers": {
                "home": home.headers if home is not None else {},
                "result": search_page.headers,
            },
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


def _primary_norm_link(cell: HtmlNode, *, page_url: str) -> JsonDict | None:
    fallback: JsonDict | None = None
    for anchor in cell.iter("a"):
        href = clean_text(anchor.get("href"))
        infoleg_id = _infoleg_id_from_href(href, page_url=page_url)
        if infoleg_id is None:
            continue
        url = f"{DETAIL_URL}?id={infoleg_id}"
        item = {
            "infoleg_id": infoleg_id,
            "url": url,
            "href": href,
            "label": anchor.text() or url,
        }
        if not _is_highlight_url(href):
            return item
        fallback = item
    return fallback


def _infoleg_id_from_href(href: str | None, *, page_url: str) -> str | None:
    url = absolute_url(page_url, href)
    if url is None:
        return None
    parsed = urlparse(url)
    path = re.sub(r";jsessionid=[^/?#;]*", "", parsed.path, flags=re.IGNORECASE)
    if not path.endswith("/verNorma.do"):
        return None
    ids = parse_qs(parsed.query).get("id")
    if not ids:
        return None
    value = clean_text(ids[0])
    return value if value and value.isdigit() else None


def _is_highlight_url(href: str | None) -> bool:
    if href is None:
        return False
    return "resaltar=true" in href.lower()


def _description_parts(cell: HtmlNode) -> JsonDict:
    lines = _node_lines(cell)
    heading = _first_tag_text(cell, "b") or (lines[0] if lines else None)
    summary = _first_tag_text(cell, "span") or (lines[-1] if len(lines) > 1 else None)
    description = cell.text()
    snippet = " - ".join(line for line in lines if line)
    return {
        "heading": heading,
        "description": description,
        "summary": summary,
        "snippet": snippet or description,
    }


def _parse_norm_label(label: str) -> JsonDict:
    text = clean_text(label) or label
    match = _NORM_LABEL_RE.match(text)
    if not match:
        return {"type": text}
    number = clean_text((match.group("number") or "").replace(" ", ""))
    return {
        "type": clean_text(match.group("kind")),
        "number": number,
    }


def _agency_from_lines(lines: list[str], *, label: str) -> str | None:
    label_key = _key(label)
    label_text = clean_text(label) or ""
    for line in lines:
        line_text = clean_text(line)
        if label_text and line_text and line_text.casefold().startswith(label_text.casefold()):
            candidate = clean_text(line_text[len(label_text) :])
            if candidate:
                return candidate
        line_key = _key(line)
        if not line_key or line_key == label_key or "ver norma" in line_key:
            continue
        return line_text
    return None


def _node_lines(node: HtmlNode) -> list[str]:
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        text = clean_text(" ".join(current))
        current.clear()
        if text:
            lines.append(text)

    def walk(value: HtmlNode | str) -> None:
        if isinstance(value, str):
            current.append(value)
            return
        if value.tag == "br":
            flush()
            return
        for child in value.children:
            walk(child)
        if value.tag in {"p", "div", "li"}:
            flush()

    for child in node.children:
        walk(child)
    flush()
    return lines


def _first_anchor_text(node: HtmlNode) -> str | None:
    for anchor in node.iter("a"):
        text = anchor.text()
        if text:
            return text
    return None


def _first_anchor_url(node: HtmlNode, *, page_url: str) -> str | None:
    for anchor in node.iter("a"):
        url = absolute_url(page_url, anchor.get("href"))
        if url:
            return url
    return None


def _first_tag_text(node: HtmlNode, tag: str) -> str | None:
    found = node.find(tag)
    return found.text() if found is not None else None


def _first_node_text(root: HtmlNode, tag: str) -> str | None:
    node = root.find(tag)
    return node.text() if node is not None else None


def _first_class_text(root: HtmlNode, class_name: str) -> str | None:
    for node in root.iter():
        classes = (node.get("class") or "").split()
        if class_name in classes:
            text = node.text()
            if text:
                return text
    return None


def _direct_children(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _publication_date_fields(value: Any, *, prefix: str) -> JsonDict:
    parsed = _parse_iso_date(value)
    if parsed is None:
        return {}
    return {
        f"diaPub{prefix}": str(parsed.day),
        f"mesPub{prefix}": str(parsed.month),
        f"anioPub{prefix}": str(parsed.year),
    }


def _iso_date_string(value: Any, *, flag: str) -> str | None:
    parsed = _parse_iso_date(value, flag=flag)
    return parsed.isoformat() if parsed else None


def _parse_iso_date(value: Any, *, flag: str | None = None) -> date | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        label = flag or "publication date"
        raise usage_error(f"{label} must be an ISO date YYYY-MM-DD", details={"value": text}) from exc


def _year(value: str) -> int:
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a year") from exc
    if year < 1800 or year > 2100:
        raise argparse.ArgumentTypeError("must be between 1800 and 2100")
    return year


def _page_number(value: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if page < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return page


def _digits_to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    return int(digits) if digits else None


def _has_more(
    *,
    total: int | None,
    total_pages: int | None,
    current_page: int | None,
    available_count: int,
    returned_count: int,
) -> bool:
    if returned_count < available_count:
        return True
    if total_pages is not None and current_page is not None:
        return current_page < total_pages
    if total is not None:
        return returned_count < total
    return returned_count < available_count


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
        source_urls=[HOME_URL, SEARCH_URL, DETAIL_URL, LINKS_URL],
        fetched_urls=[_public_url(url) for url in fetched_urls],
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _public_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r";jsessionid=[^/?#;]*", "", parts.path, flags=re.IGNORECASE)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _key(value: Any) -> str:
    return _static_key(value)


register_adapter(build_adapter(), replace=True)
