"""IGJ official-page and SAIJ-backed resolution adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from apps.legal.errors import not_found, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import LegalDocument, LegalItem, LegalResponse, Provenance
from apps.legal.pagination import build_page_info, decode_cursor
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
from apps.legal.sources import saij


SOURCE_ID = "igj"
SOURCE_MAP = "apps/legal/docs/igj_resoluciones.md"

ARGENTINA_BASE_URL = "https://www.argentina.gob.ar"
MARCO_NORMATIVO_URL = f"{ARGENTINA_BASE_URL}/justicia/igj/institucional/marco-normativo"
YEAR_URL_TEMPLATE = (
    f"{ARGENTINA_BASE_URL}/justicia/igj/marco-normativo-igj/"
    "resoluciones-generales-ano-{year}"
)
YEAR_URL_OVERRIDES = {
    2022: f"{ARGENTINA_BASE_URL}/marco-normativo-igj/resoluciones-generales-ano-2022",
    2021: f"{ARGENTINA_BASE_URL}/justicia/igj/resolucionesgenerales2021",
}

IGJ_SAIJ_URL = "https://www.saij.gob.ar/buscador/resoluciones-igj"
IGJ_SAIJ_FACET = "Organismo/IGJ"
IGJ_SAIJ_FACETS = (
    "Total|Tipo de Documento|Fecha|Organismo/IGJ|Publicación|Tema|"
    "Estado de Vigencia|Autor|Jurisdicción"
)

INFOLEG_BASE_URL = "https://servicios.infoleg.gob.ar/infolegInternet/"
INFOLEG_DETAIL_URL = f"{INFOLEG_BASE_URL}verNorma.do"

DEFAULT_LIMIT = 25
DEFAULT_OFFSET = 0
SNIPPET_LENGTH = 360

JsonDict = dict[str, Any]

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_RESOLUTION_TEXT_RE = re.compile(
    r"(?:resoluci[oó]n\s+(?:general\s+)?(?:igj\s+)?|rg\s+(?:igj\s+)?)"
    r"(?:n(?:ro|[°º])?\.?\s*)?\d+",
    re.IGNORECASE,
)
_RESOLUTION_NUMBER_RE = re.compile(
    r"(?:resoluci[oó]n\s+general\s+igj|resoluci[oó]n\s+general|rg\s+igj|rg)"
    r"\s*(?:n(?:ro|[°º])?\.?\s*)?(?P<number>\d+)\s*/\s*(?P<year>\d{2,4})",
    re.IGNORECASE,
)
_RESOG_RE = re.compile(r"resog-(?P<year>\d{4})-(?P<number>\d+).*igj", re.IGNORECASE)
_URL_RG_RE = re.compile(r"(?:rg[_-]?igj|resog|resolucion).*?\d", re.IGNORECASE)
_URL_RG_NUMBER_RE = re.compile(r"rg[_-]?igj[_-](?P<number>\d+)[-_](?P<year>\d{2,4})", re.IGNORECASE)
_INFOLEG_ID_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class OfficialPage:
    url: str
    items: list[LegalItem]
    headers: JsonDict
    year: int | None = None


@dataclass(frozen=True)
class InfolegDetail:
    infoleg_id: str
    url: str
    html: str
    headers: JsonDict


def add_list_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--year", type=_year, help="official IGJ resolution year")


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text query mapped to SAIJ texto")
    parser.add_argument("--raw-query", dest="raw_query", help="SAIJ raw query expression passed as r")
    parser.add_argument("--facets", help="pipe-separated SAIJ facets; Organismo/IGJ is always enforced")
    parser.add_argument("--offset", type=_non_negative_int, help="zero-based SAIJ search offset")
    parser.add_argument("--sort", help="SAIJ sort expression passed as s")


def add_get_infoleg_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="infoleg_id", help="Infoleg norma id")


def add_scrape_official_page_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="Argentina.gob.ar IGJ official page URL")
    parser.add_argument("--year", type=_year, help="optional year to include in normalized records")


def handle_list(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="list")
    if cursor_payload.get("year") is None and args.year is None:
        raise usage_error("--year is required")
    year = int(cursor_payload.get("year") or args.year)
    page_url = official_year_url(year)

    with _make_client() as client:
        official_page = fetch_official_page(page_url, client=client, year=year)

    return _official_page_response(
        official_page,
        operation="list",
        query={"year": year, "url": page_url},
        limit=args.limit,
        cursor_payload=cursor_payload,
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    offset = int(args.offset if args.offset is not None else cursor_payload.get("offset", DEFAULT_OFFSET))

    with _make_saij_client() as client:
        search_page = saij.fetch_search_page(
            raw_query=query["raw_query"],
            facets=query["facets"],
            offset=offset,
            limit=limit,
            sort=query.get("sort"),
            client=client,
        )

    items = [
        _saij_hit_to_igj_item(hit, search_page=search_page, include_raw=bool(args.raw))
        for hit in search_page.hits
    ]
    has_more = _has_more(total=search_page.total, offset=offset, limit=limit, item_count=len(items))
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "offset": offset, "limit": limit},
        items=items,
        page=build_page_info(
            source=SOURCE_ID,
            operation="search",
            offset=offset,
            limit=limit,
            total=search_page.total,
            item_count=len(items),
            has_more=has_more,
            raw={"query": query} if has_more else None,
        ),
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            raw={
                "headers": search_page.headers,
                "queryObjectData": search_page.query_object,
            },
        ),
        facets=search_page.facets,
    )


def handle_get_infoleg(args: argparse.Namespace) -> LegalResponse:
    infoleg_id = _required_infoleg_id(args.infoleg_id)

    with _make_client() as client:
        detail = fetch_infoleg_detail(infoleg_id, client=client)

    document = infoleg_detail_to_document(detail, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get-infoleg",
        request={"id": infoleg_id},
        document=document,
        provenance=document.provenance,
    )


def handle_scrape_official_page(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="scrape-official-page")
    if cursor_payload.get("url") is None and not args.url:
        raise usage_error("--url is required")
    page_url = str(cursor_payload.get("url") or args.url)
    year = cursor_payload.get("year") if cursor_payload else args.year
    normalized_year = int(year) if year is not None else None

    with _make_client() as client:
        official_page = fetch_official_page(page_url, client=client, year=normalized_year)

    return _official_page_response(
        official_page,
        operation="scrape-official-page",
        query={"url": page_url, "year": normalized_year},
        limit=args.limit,
        cursor_payload=cursor_payload,
    )


def official_year_url(year: int) -> str:
    return YEAR_URL_OVERRIDES.get(year, YEAR_URL_TEMPLATE.format(year=year))


def fetch_official_page(
    url: str,
    *,
    client: LegalHttpClient | None = None,
    year: int | None = None,
) -> OfficialPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", url)
        items = parse_official_page_html(response.text, page_url=str(response.url), year=year)
        return OfficialPage(
            url=str(response.url),
            items=items,
            headers=_useful_headers(response),
            year=year,
        )
    finally:
        if owns_client:
            http.close()


def parse_official_page_html(html: str, *, page_url: str, year: int | None = None) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()

    for index, anchor in enumerate(root.iter("a"), start=1):
        href = anchor.get("href")
        url = _official_href_url(page_url, href)
        if url is None or url in seen_urls:
            continue
        label = _anchor_label(anchor, url=url)
        context = _nearest_context_text(anchor)
        target_type = classify_official_target(url)
        if not _is_official_resolution_link(label=label, url=url, context=context, target_type=target_type):
            continue

        seen_urls.add(url)
        item = _official_link_to_item(
            url=url,
            label=label,
            context=context,
            page_url=page_url,
            year=year,
            target_type=target_type,
            index=index,
            seen_ids=seen_ids,
        )
        items.append(item)
    return items


def classify_official_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "infoleg" in host or "infoleginternet" in path:
        return "infoleg"
    if host.endswith("saij.gob.ar"):
        return "saij"
    if path.endswith(".pdf"):
        return "pdf"
    return "other"


def fetch_infoleg_detail(
    infoleg_id: str,
    *,
    client: LegalHttpClient | None = None,
) -> InfolegDetail:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", INFOLEG_DETAIL_URL, params={"id": infoleg_id})
        if not response.text.strip():
            raise not_found(
                "Infoleg detail page was empty",
                details={"id": infoleg_id},
                provenance=_provenance(fetched_urls=[str(response.url)], source_response_id=infoleg_id),
            )
        return InfolegDetail(
            infoleg_id=infoleg_id,
            url=str(response.url),
            html=response.text,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def infoleg_detail_to_document(detail: InfolegDetail, *, include_raw: bool = False) -> LegalDocument:
    root = parse_html(detail.html)
    body = text_content(root)
    if body is None:
        raise parse_error(
            "Infoleg detail page did not contain visible text",
            details={"id": detail.infoleg_id},
            provenance=_provenance(fetched_urls=[detail.url], source_response_id=detail.infoleg_id),
        )

    title = _infoleg_title(root, fallback=detail.infoleg_id)
    header = _first_node_text(root, "strong")
    agency = _first_class_text(root, "destacado")
    date = _infoleg_date(root, body)
    links = _infoleg_links(detail.html, page_url=detail.url)
    metadata: JsonDict = {
        "id": detail.infoleg_id,
        "header": header,
        "agency": agency,
        "number": _infoleg_number(header),
        "publication_date": date,
        "original_text_url": _first_link_url(links, "original_text"),
        "updated_text_url": _first_link_url(links, "updated_text"),
    }

    return LegalDocument(
        id=f"{SOURCE_ID}:infoleg:{detail.infoleg_id}",
        title=title,
        date=date,
        document_type=_infoleg_document_type(header),
        body=body,
        url=detail.url,
        content_type="text/html",
        text_format="plain_text",
        metadata={key: value for key, value in metadata.items() if value not in (None, "", [])},
        links=links,
        source_fields={"infoleg_id": detail.infoleg_id, "source": "infoleg"},
        raw={"html": detail.html, "headers": detail.headers} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[detail.url],
            source_response_id=detail.infoleg_id,
            raw={"headers": detail.headers},
        ),
    )


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("IGJ source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("list", handle_list, help="list official IGJ resolutions by year", add_arguments=add_list_arguments)
    adapter.register_operation("search", handle_search, help="search IGJ resolutions via SAIJ", add_arguments=add_search_arguments)
    adapter.register_operation(
        "get-infoleg",
        handle_get_infoleg,
        help="fetch an IGJ-linked Infoleg detail page by id",
        add_arguments=add_get_infoleg_arguments,
    )
    adapter.register_operation(
        "scrape-official-page",
        handle_scrape_official_page,
        help="scrape an Argentina.gob.ar IGJ official page",
        add_arguments=add_scrape_official_page_arguments,
    )
    return adapter


def _official_page_response(
    official_page: OfficialPage,
    *,
    operation: str,
    query: JsonDict,
    limit: int | None,
    cursor_payload: Mapping[str, Any],
) -> LegalResponse:
    offset = int(cursor_payload.get("offset") or DEFAULT_OFFSET)
    page_limit = int(limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    items = official_page.items[offset : offset + page_limit]
    has_more = offset + len(items) < len(official_page.items)
    clean_query = {key: value for key, value in query.items() if value is not None}
    return LegalResponse.search(
        source=SOURCE_ID,
        operation=operation,
        query={**clean_query, "offset": offset, "limit": page_limit},
        items=items,
        page=build_page_info(
            source=SOURCE_ID,
            operation=operation,
            offset=offset,
            limit=page_limit,
            total=len(official_page.items),
            item_count=len(items),
            has_more=has_more,
            raw={"url": official_page.url, "year": official_page.year} if has_more else None,
        ),
        provenance=_provenance(
            fetched_urls=[official_page.url],
            raw={
                "headers": official_page.headers,
                "item_count": len(official_page.items),
            },
        ),
    )


def _saij_hit_to_igj_item(
    hit: Mapping[str, Any],
    *,
    search_page: saij.SaijSearchPage,
    include_raw: bool = False,
) -> LegalItem:
    source_item = saij.hit_to_item(hit, search_page=search_page, include_raw=include_raw)
    source_fields = dict(source_item.source_fields)
    guid = _optional_text(source_fields.get("guid") or source_fields.get("uuid")) or source_item.id.rsplit(":", 1)[-1]
    source_fields.update(
        {
            "source": "saij",
            "saij_id": source_item.id,
            "guid": guid,
            "organismo_facet": IGJ_SAIJ_FACET,
        }
    )
    facets = dict(source_item.facets)
    facets["organismo"] = "IGJ"
    facets["source"] = "SAIJ"
    return LegalItem(
        id=f"{SOURCE_ID}:{guid}",
        title=source_item.title,
        date=source_item.date,
        document_type=source_item.document_type,
        url=source_item.url,
        file_url=source_item.file_url,
        snippet=source_item.snippet,
        facets=facets,
        source_fields=source_fields,
        raw=source_item.raw,
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=guid,
            raw={
                "documentScore": hit.get("documentScore"),
                "saij_item_id": source_item.id,
            },
        ),
    )


def _official_link_to_item(
    *,
    url: str,
    label: str,
    context: str | None,
    page_url: str,
    year: int | None,
    target_type: str,
    index: int,
    seen_ids: set[str],
) -> LegalItem:
    resolution = _resolution_ref(label, url=url, context=context)
    infoleg_id = _infoleg_id_from_url(url)
    saij_guid = _saij_guid_from_url(url)
    kind = "pdf" if target_type == "pdf" else classify_link(url, base_url=page_url)
    document_type = "annex" if "anexo" in _search_key(label) else "resolution"
    item_id = _official_item_id(
        url=url,
        label=label,
        target_type=target_type,
        resolution=resolution,
        infoleg_id=infoleg_id,
        saij_guid=saij_guid,
        index=index,
        seen_ids=seen_ids,
    )
    item_year = resolution.get("year") or year
    source_fields: JsonDict = {
        "label": label,
        "url": url,
        "target_type": target_type,
        "kind": kind,
        "year": item_year,
        "number": resolution.get("number"),
        "infoleg_id": infoleg_id,
        "saij_guid": saij_guid,
        "context": context,
    }
    return LegalItem(
        id=item_id,
        title=label,
        date=normalize_date(context),
        document_type=document_type,
        url=url,
        file_url=url if target_type == "pdf" else None,
        snippet=clean_snippet(context, max_length=SNIPPET_LENGTH),
        facets={
            key: value
            for key, value in {
                "target_type": target_type,
                "kind": kind,
                "year": item_year,
                "number": resolution.get("number"),
            }.items()
            if value is not None
        },
        source_fields={key: value for key, value in source_fields.items() if value not in (None, "", [])},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=item_id,
            raw={"href": url, "index": index},
        ),
    )


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    text = _optional_text(args.text)
    raw_query = _optional_text(args.raw_query)
    if raw_query is None:
        if text is None:
            raise usage_error("either --text or --raw-query is required")
        raw_query = f"texto:{text}"

    query: JsonDict = {
        "text": text,
        "raw_query": raw_query,
        "facets": _facets_with_igj(args.facets),
        "sort": _optional_text(args.sort),
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _facets_with_igj(value: Any) -> str:
    parts = _split_facets(value) or _split_facets(IGJ_SAIJ_FACETS)
    output: list[str] = []
    replaced = False
    for part in parts:
        if _facet_root_key(part) == "organismo":
            if not replaced:
                output.append(IGJ_SAIJ_FACET)
                replaced = True
            continue
        output.append(part)
    if not replaced:
        output.append(IGJ_SAIJ_FACET)
    return "|".join(output)


def _split_facets(value: Any) -> list[str]:
    text = _optional_text(value)
    if not text:
        return []
    return [part for part in (clean_text(item) for item in text.split("|")) if part]


def _facet_root_key(value: str) -> str:
    return _search_key(value.split("/", 1)[0])


def _official_href_url(page_url: str, href: str | None) -> str | None:
    raw = clean_text(href)
    if raw is None:
        return None
    if raw.startswith("blank:#"):
        raw = raw.removeprefix("blank:#")
    elif raw.startswith("blank:"):
        raw = raw.removeprefix("blank:")
    if raw.startswith("#") or raw == "":
        return None
    return absolute_url(page_url, raw)


def _anchor_label(anchor: HtmlNode, *, url: str) -> str:
    for value in (anchor.text(), anchor.get("title"), anchor.get("aria-label")):
        label = _optional_text(value)
        if label:
            return label
    return _filename_from_url(url) or url


def _nearest_context_text(anchor: HtmlNode) -> str | None:
    current: HtmlNode | None = anchor
    while current is not None:
        if current.tag in {"p", "li", "td", "tr", "div"}:
            text = current.text()
            if text:
                return text
        current = current.parent
    return anchor.text()


def _is_official_resolution_link(*, label: str, url: str, context: str | None, target_type: str) -> bool:
    combined = " ".join(part for part in (label, context or "", url) if part)
    search_key = _search_key(combined)
    if "resoluciones generales ano" in search_key:
        return False
    if _RESOLUTION_TEXT_RE.search(combined) or _RESOG_RE.search(combined):
        return True
    if target_type in {"infoleg", "pdf", "saij"} and _URL_RG_RE.search(url):
        return True
    if target_type == "pdf" and "igj" in search_key and "anexo" in search_key:
        return True
    return False


def _resolution_ref(label: str, *, url: str, context: str | None) -> JsonDict:
    match = (
        _URL_RG_NUMBER_RE.search(url)
        or _RESOG_RE.search(url)
        or _RESOLUTION_NUMBER_RE.search(label)
        or _RESOLUTION_NUMBER_RE.search(context or "")
        or _RESOG_RE.search(context or "")
    )
    if not match:
        return {}
    return {
        "number": str(int(match.group("number"))),
        "year": _full_year(match.group("year")),
    }


def _official_item_id(
    *,
    url: str,
    label: str,
    target_type: str,
    resolution: Mapping[str, Any],
    infoleg_id: str | None,
    saij_guid: str | None,
    index: int,
    seen_ids: set[str],
) -> str:
    if infoleg_id:
        base = f"{SOURCE_ID}:infoleg:{infoleg_id}"
    elif saij_guid:
        base = f"{SOURCE_ID}:saij:{saij_guid}"
    elif resolution.get("year") and resolution.get("number"):
        base = f"{SOURCE_ID}:resolution:{resolution['year']}:{resolution['number']}"
        if "anexo" in _search_key(label):
            base = f"{base}:{_slug(label)}"
    else:
        filename = _filename_from_url(url)
        base = f"{SOURCE_ID}:official:{target_type}:{_slug(filename or label) or index}"

    item_id = base
    counter = 2
    while item_id in seen_ids:
        item_id = f"{base}:{counter}"
        counter += 1
    seen_ids.add(item_id)
    return item_id


def _infoleg_links(html: str, *, page_url: str) -> list[JsonDict]:
    links: list[JsonDict] = []
    for link in extract_links(html, base_url=page_url):
        url = _optional_text(link.get("url"))
        if url is None:
            continue
        label = _optional_text(link.get("label")) or url
        target_type = _infoleg_link_type(label=label, url=url)
        normalized = dict(link)
        normalized["target_type"] = target_type
        if "kind" not in normalized:
            normalized["kind"] = classify_link(url, base_url=page_url)
        links.append({key: value for key, value in normalized.items() if value not in (None, "")})
    return _dedupe_links(links)


def _infoleg_link_type(*, label: str, url: str) -> str:
    search_key = _search_key(f"{label} {url}")
    if "norma htm" in search_key or "texto completo" in search_key:
        return "original_text"
    if "texact htm" in search_key or "texto actualizado" in search_key:
        return "updated_text"
    if "vervinculos" in search_key:
        return "relationships"
    return classify_official_target(url)


def _dedupe_links(links: list[JsonDict]) -> list[JsonDict]:
    output: list[JsonDict] = []
    seen: set[str] = set()
    for link in links:
        url = _optional_text(link.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        output.append(link)
    return output


def _infoleg_title(root: HtmlNode, *, fallback: str) -> str:
    h1 = root.find("h1")
    h1_text = h1.text() if h1 is not None else None
    header = _first_node_text(root, "strong")
    if header and h1_text:
        return f"{header} - {h1_text}"
    return h1_text or header or fallback


def _infoleg_date(root: HtmlNode, body: str) -> str | None:
    for span in root.iter("span"):
        classes = span.get("class") or ""
        if "vr_azul11" in classes:
            text = span.text()
            normalized = normalize_date(text.replace("-", " ") if text else None)
            if normalized:
                return normalized
    return normalize_date(body)


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


def _first_link_url(links: list[JsonDict], target_type: str) -> str | None:
    for link in links:
        if link.get("target_type") == target_type:
            return _optional_text(link.get("url"))
    return None


def _infoleg_document_type(header: str | None) -> str | None:
    if not header:
        return None
    match = re.match(r"(?P<kind>[^\d]+?)\s+\d", header)
    return clean_text(match.group("kind")) if match else clean_text(header)


def _infoleg_number(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r"\b(?P<number>\d+\s*/\s*\d{2,4})\b", header)
    return match.group("number").replace(" ", "") if match else None


def _infoleg_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get("id")
    if values:
        return _optional_text(values[0])
    match = re.search(r"/(?P<id>\d+)/(?:norma|texact)\.htm$", parsed.path, re.IGNORECASE)
    return match.group("id") if match else None


def _saij_guid_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    for key in ("guid", "id"):
        values = parse_qs(parsed.query).get(key)
        if values and _optional_text(values[0]):
            return _optional_text(values[0])
    return None


def _required_infoleg_id(value: Any) -> str:
    infoleg_id = _optional_text(value)
    if not infoleg_id:
        raise usage_error("--id is required")
    if not _INFOLEG_ID_RE.fullmatch(infoleg_id):
        raise usage_error("--id must be a numeric Infoleg norma id", details={"id": infoleg_id})
    return infoleg_id


def _year(value: str) -> int:
    try:
        year = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a year") from exc
    if year < 1900 or year > 2100:
        raise argparse.ArgumentTypeError("must be between 1900 and 2100")
    return year


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _full_year(value: str) -> int:
    year = int(value)
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _filename_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    return path.rsplit("/", 1)[-1] or None


def _slug(value: Any) -> str:
    text = _search_key(value)
    return text.replace(" ", "-")


def _search_key(value: Any) -> str:
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


def _has_more(*, total: int | None, offset: int, limit: int, item_count: int) -> bool:
    if total is None:
        return item_count >= limit
    return offset + item_count < total


def _useful_headers(response: httpx.Response) -> JsonDict:
    return {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "etag", "last-modified"}
    }


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Referer": MARCO_NORMATIVO_URL})


def _make_saij_client() -> LegalHttpClient:
    return LegalHttpClient(
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": IGJ_SAIJ_URL,
        }
    )


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[MARCO_NORMATIVO_URL, IGJ_SAIJ_URL, INFOLEG_BASE_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


register_adapter(build_adapter(), replace=True)
