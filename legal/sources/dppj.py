"""DPPJ static legislation page adapter."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from legal.errors import LegalCliError, not_found, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, Provenance
from legal.pagination import build_page_info, decode_cursor
from legal.parsing import (
    HtmlNode,
    absolute_url,
    classify_link,
    clean_snippet,
    clean_text,
    parse_html,
)
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter
from legal.sources import normas_pba


SOURCE_ID = "dppj"
SOURCE_MAP = "apps/legal/docs/dppj_resoluciones.md"

BASE_URL = "https://www.gba.gob.ar"
LEGISLACION_URL = f"{BASE_URL}/dppj/legislacion"

DEFAULT_LIMIT = 25
DEFAULT_OFFSET = 0
SNIPPET_LENGTH = 420

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DISPOSITION_DIRECT_RE = re.compile(
    r"\bdisposici[oó]n\s+(?:n(?:ro|[°º])?\.?\s*)?(?P<number>\d+)\s*/\s*(?P<year>\d{2,4})",
    re.IGNORECASE,
)
_DISPOSITION_LATER_RE = re.compile(
    r"\bdisposici[oó]n\b(?P<body>.{0,140}?)\b(?P<number>\d+)\s*/\s*(?P<year>\d{2,4})",
    re.IGNORECASE,
)
_DISPOSITION_FILE_RE = re.compile(
    r"\b(?:di|dis)[-_]?(?P<year>\d{4})[-_](?P<number>\d+)\b|"
    r"\b(?:di|dis)[-_](?P<number2>\d+)[-_](?P<year2>\d{2,4})\b",
    re.IGNORECASE,
)
_INFOLEG_ID_RE = re.compile(r"/(?P<id>\d+)/(?:norma|texact)\.htm$", re.IGNORECASE)

KIND_ALIASES: Mapping[str, str] = {
    "disposicion": "disposition",
    "disposition": "disposition",
    "ley": "law",
    "law": "law",
    "decreto": "decree",
    "decree": "decree",
    "decreto ley": "decree-law",
    "decree law": "decree-law",
    "decree-law": "decree-law",
    "anexo": "annex",
    "annex": "annex",
    "formulario": "form",
    "form": "form",
    "pdf": "pdf",
}


@dataclass(frozen=True)
class DppjPage:
    url: str
    html: str
    items: list[LegalItem]
    headers: JsonDict


@dataclass(frozen=True)
class NormasFallback:
    document: LegalDocument | None = None
    item: LegalItem | None = None
    fetched_urls: tuple[str, ...] = ()
    warning: str | None = None


def add_list_arguments(parser: argparse.ArgumentParser) -> None:
    _add_filter_arguments(parser)


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    _add_filter_arguments(parser)
    parser.add_argument("--number", help="DPPJ disposition number")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--number", help="DPPJ disposition number")
    parser.add_argument("--year", type=_year, help="DPPJ disposition year")


def add_sync_arguments(parser: argparse.ArgumentParser) -> None:
    _add_filter_arguments(parser)
    parser.add_argument("--out", help="optional JSONL output path")


def handle_list(args: argparse.Namespace) -> LegalResponse:
    return _handle_static_items(args, operation="list")


def handle_search(args: argparse.Namespace) -> LegalResponse:
    return _handle_static_items(args, operation="search")


def handle_sync(args: argparse.Namespace) -> LegalResponse:
    response = _handle_static_items(args, operation="sync", default_limit=None)
    out_path = _optional_text(args.out)
    if out_path:
        path = Path(out_path).expanduser()
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for item in response.items or []:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        response = LegalResponse.search(
            source=SOURCE_ID,
            operation="sync",
            query=response.query,
            items=response.items,
            page=response.page,
            provenance=response.provenance,
            facets=response.facets,
            warnings=[*response.warnings, f"wrote JSONL to {path}"],
        )
    return response


def handle_get(args: argparse.Namespace) -> LegalResponse:
    number = _required_number(args.number)
    year = _required_year(args.year)

    with _make_client() as client:
        official_page = fetch_legislation_page(client=client, include_raw=bool(args.raw))

    official_matches = [
        item
        for item in official_page.items
        if _item_number(item) == number and _item_year(item) == year and _kind_matches(item, "disposition")
    ]
    primary = _preferred_official_match(official_matches)
    fallback = _normas_fallback(number=number, year=year, primary=primary, include_raw=bool(args.raw))

    if primary is None and fallback.document is None:
        raise not_found(
            "DPPJ disposition was not found on the official list or Normas PBA",
            details={"number": number, "year": year},
            provenance=_provenance(fetched_urls=[official_page.url], source_response_id=_disposition_id(number, year)),
        )

    warnings = [fallback.warning] if fallback.warning else []
    document = _get_document(
        number=number,
        year=year,
        official_page=official_page,
        official_matches=official_matches,
        primary=primary,
        fallback=fallback,
        include_raw=bool(args.raw),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={"number": number, "year": year},
        document=document,
        provenance=document.provenance,
        warnings=warnings,
    )


def fetch_legislation_page(
    *,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> DppjPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", LEGISLACION_URL)
        items = parse_legislation_html(response.text, page_url=str(response.url), include_raw=include_raw)
        if not items:
            raise parse_error(
                "DPPJ legislation page did not contain recognized official links",
                details={"url": str(response.url)},
                provenance=_provenance(fetched_urls=[str(response.url)]),
            )
        return DppjPage(
            url=str(response.url),
            html=response.text,
            items=items,
            headers=_useful_headers(response),
        )
    finally:
        if owns_client:
            http.close()


def parse_legislation_html(html: str, *, page_url: str, include_raw: bool = False) -> list[LegalItem]:
    root = parse_html(html)
    items: list[LegalItem] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()

    for index, anchor in enumerate(root.iter("a"), start=1):
        url = _href_url(page_url, anchor.get("href"))
        if url is None or url in seen_urls:
            continue

        label = _anchor_label(anchor, url=url)
        if label is None:
            continue

        context = _nearest_context_text(anchor)
        target_type = classify_target(url)
        if not _is_legislation_link(label=label, context=context, target_type=target_type):
            continue

        seen_urls.add(url)
        item = _link_to_item(
            url=url,
            label=label,
            context=context,
            page_url=page_url,
            target_type=target_type,
            index=index,
            include_raw=include_raw,
            seen_ids=seen_ids,
        )
        items.append(item)
    return items


def classify_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "normas.gba.gob.ar":
        if normas_pba.parse_detail_route(url) is not None:
            return "normas_pba_detail"
        if path.startswith("/resultados"):
            return "normas_pba_search"
        return "normas_pba"
    if host == "drive.mjus.gba.gob.ar" and path.startswith("/docs/dppj/"):
        return "dppj_pdf" if path.endswith(".pdf") else "dppj_file"
    if "infoleg" in host or "infoleginternet" in path:
        return "infoleg"
    if host == "drive.google.com":
        return "external_google_drive"
    return "external" if host else "relative"


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("DPPJ source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("list", handle_list, help="list DPPJ official legislation links", add_arguments=add_list_arguments)
    adapter.register_operation("search", handle_search, help="search DPPJ official legislation links", add_arguments=add_search_arguments)
    adapter.register_operation("get", handle_get, help="fetch a DPPJ disposition by number and year", add_arguments=add_get_arguments)
    adapter.register_operation("sync", handle_sync, help="sync DPPJ official legislation links", add_arguments=add_sync_arguments)
    return adapter


def _handle_static_items(
    args: argparse.Namespace,
    *,
    operation: str,
    default_limit: int | None = DEFAULT_LIMIT,
) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation=operation)
    query = _query_from_args(args, cursor_payload=cursor_payload)
    offset = int(cursor_payload.get("offset") or DEFAULT_OFFSET)
    page_limit = _response_limit(args.limit, cursor_payload=cursor_payload, default_limit=default_limit)

    with _make_client() as client:
        official_page = fetch_legislation_page(client=client, include_raw=bool(args.raw))

    filtered_items = _filter_items(official_page.items, query)
    items = filtered_items[offset:] if page_limit is None else filtered_items[offset : offset + page_limit]
    has_more = False if page_limit is None else offset + len(items) < len(filtered_items)
    return LegalResponse.search(
        source=SOURCE_ID,
        operation=operation,
        query={**query, "offset": offset, "limit": page_limit},
        items=items,
        page=build_page_info(
            source=SOURCE_ID,
            operation=operation,
            offset=offset,
            limit=page_limit,
            page=1,
            total=len(filtered_items),
            item_count=len(items),
            has_more=has_more,
            raw={"query": query} if has_more else None,
        ),
        provenance=_provenance(
            fetched_urls=[official_page.url],
            raw={"headers": official_page.headers, "item_count": len(official_page.items)},
        ),
        facets={
            "kinds": sorted(set(_item_kind(item) for item in official_page.items if _item_kind(item))),
            "target_types": sorted(set(_optional_text(item.source_fields.get("target_type")) for item in official_page.items if item.source_fields.get("target_type"))),
        },
    )


def _link_to_item(
    *,
    url: str,
    label: str,
    context: str | None,
    page_url: str,
    target_type: str,
    index: int,
    include_raw: bool,
    seen_ids: set[str],
) -> LegalItem:
    route = normas_pba.parse_detail_route(url)
    disposition = _disposition_ref(label=label, context=context, url=url, route=route)
    document_type = _document_type(label=label, context=context, route=route)
    number = disposition.get("number") or (route.number if route and document_type == "disposition" else None)
    year = disposition.get("year") or (route.year if route else None)
    kind = _link_kind(url=url, page_url=page_url, document_type=document_type, target_type=target_type)
    item_id = _item_id(
        label=label,
        url=url,
        target_type=target_type,
        document_type=document_type,
        route=route,
        number=number,
        year=year,
        index=index,
        seen_ids=seen_ids,
    )
    source_fields = _compact(
        {
            "source_title": label,
            "source_text": context,
            "url": url,
            "target_type": target_type,
            "kind": kind,
            "number": number,
            "year": year,
            "normas_pba_path": route.path if route else None,
            "normas_pba_route": route.to_dict() if route else None,
            "normas_pba_search": _normas_search_fields(url) if target_type == "normas_pba_search" else None,
            "infoleg_id": _infoleg_id(url),
        }
    )
    return LegalItem(
        id=item_id,
        title=label,
        document_type=document_type,
        url=url,
        file_url=url if kind in {"pdf", "document", "spreadsheet"} else None,
        snippet=clean_snippet(context, max_length=SNIPPET_LENGTH),
        facets=_compact({"kind": document_type, "target_type": target_type, "number": number, "year": year}),
        source_fields=source_fields,
        raw={"index": index, "href": url} if include_raw else {},
        provenance=_provenance(fetched_urls=[page_url], source_response_id=item_id, raw={"index": index, "href": url}),
    )


def _normas_fallback(
    *,
    number: str,
    year: str,
    primary: LegalItem | None,
    include_raw: bool,
) -> NormasFallback:
    fetched_urls: list[str] = []
    route = _route_from_item(primary) if primary is not None else None
    if route is not None:
        try:
            detail = normas_pba.fetch_detail(route)
            fetched_urls.append(detail.url)
            return NormasFallback(
                document=normas_pba.detail_to_document(detail, include_raw=include_raw),
                fetched_urls=tuple(fetched_urls),
            )
        except LegalCliError as exc:
            fetched_urls.extend(exc.provenance.fetched_urls if exc.provenance else [])

    try:
        query = {
            "raw_type": "Disposition",
            "number": number,
            "year": year,
            "sort": "by_publication_date_desc",
        }
        search_page = normas_pba.fetch_search_page(query=query, page=1, include_raw=include_raw)
        fetched_urls.append(search_page.url)
        if not search_page.items:
            return NormasFallback(fetched_urls=tuple(fetched_urls), warning="Normas PBA fallback returned no results")
        item = search_page.items[0]
        route = _route_from_item(item)
        if route is None:
            return NormasFallback(item=item, fetched_urls=tuple(fetched_urls), warning="Normas PBA fallback result had no detail route")
        detail = normas_pba.fetch_detail(route)
        fetched_urls.append(detail.url)
        return NormasFallback(
            document=normas_pba.detail_to_document(detail, include_raw=include_raw),
            item=item,
            fetched_urls=tuple(fetched_urls),
        )
    except LegalCliError as exc:
        fetched_urls.extend(exc.provenance.fetched_urls if exc.provenance else [])
        return NormasFallback(
            fetched_urls=tuple(fetched_urls),
            warning=f"Normas PBA fallback failed: {exc.code}",
        )


def _get_document(
    *,
    number: str,
    year: str,
    official_page: DppjPage,
    official_matches: list[LegalItem],
    primary: LegalItem | None,
    fallback: NormasFallback,
    include_raw: bool,
) -> LegalDocument:
    normas_document = fallback.document
    title = primary.title if primary is not None else None
    if title is None and normas_document is not None:
        title = normas_document.title
    title = title or f"Disposicion {number}/{year}"
    official_file = _official_file(primary)
    links = _official_links(official_matches)
    if normas_document is not None:
        links.append(
            {
                "url": normas_document.url,
                "label": normas_document.title,
                "kind": "page",
                "target_type": "normas_pba_detail",
                "id": normas_document.id,
            }
        )
        links.extend(normas_document.links)

    files = [official_file] if official_file else []
    if normas_document is not None:
        files.extend(normas_document.files)

    official_source = primary.source_fields if primary is not None else {}
    fetched_urls = [official_page.url, *fallback.fetched_urls]
    if normas_document is not None and normas_document.provenance is not None:
        fetched_urls.extend(normas_document.provenance.fetched_urls)

    return LegalDocument(
        id=_disposition_id(number, year),
        title=title,
        date=normas_document.date if normas_document is not None else None,
        document_type="disposition",
        body=normas_document.body if normas_document is not None else primary.snippet if primary is not None else None,
        url=primary.url if primary is not None else normas_document.url if normas_document is not None else None,
        file_url=primary.file_url if primary is not None else normas_document.file_url if normas_document is not None else None,
        content_type=normas_document.content_type if normas_document is not None else "application/pdf" if official_file else "text/plain",
        text_format=normas_document.text_format if normas_document is not None else "plain_text",
        metadata=_compact(
            {
                "number": number,
                "year": year,
                "official_title": official_source.get("source_title"),
                "official_text": official_source.get("source_text"),
                "target_type": official_source.get("target_type"),
                "kind": official_source.get("kind"),
                "official_record_count": len(official_matches),
                "normas_pba_id": normas_document.id if normas_document is not None else None,
                "normas_pba_metadata": normas_document.metadata if normas_document is not None else None,
            }
        ),
        links=_dedupe_links(links),
        files=_dedupe_links(files),
        source_fields=_compact(
            {
                "official_records": [item.to_dict() for item in official_matches],
                "primary_official_record": primary.to_dict() if include_raw and primary is not None else official_source,
                "normas_pba": _normas_summary(normas_document),
                "normas_pba_fallback_item": fallback.item.to_dict() if include_raw and fallback.item is not None else None,
            }
        ),
        raw={"official_page_html": official_page.html} if include_raw else {},
        provenance=Provenance.now(
            source_urls=[LEGISLACION_URL, normas_pba.SEARCH_URL],
            fetched_urls=_dedupe_texts(fetched_urls),
            source_map=SOURCE_MAP,
            source_response_id=_disposition_id(number, year),
            raw={"official_headers": official_page.headers},
        ),
    )


def _filter_items(items: list[LegalItem], query: Mapping[str, Any]) -> list[LegalItem]:
    text = _optional_text(query.get("text"))
    kind = _optional_text(query.get("kind"))
    year = _optional_text(query.get("year"))
    number = _optional_text(query.get("number"))
    output: list[LegalItem] = []
    for item in items:
        if kind and not _kind_matches(item, kind):
            continue
        if year and _item_year(item) != year:
            continue
        if number and _item_number(item) != _normalize_number(number):
            continue
        if text and _search_key(text) not in _search_key(" ".join(_item_search_parts(item))):
            continue
        output.append(item)
    return output


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    query: JsonDict = {
        "text": _optional_text(getattr(args, "text", None)),
        "kind": _kind_arg(getattr(args, "kind", None)),
        "year": str(args.year) if getattr(args, "year", None) is not None else None,
        "number": _normalize_number(getattr(args, "number", None)),
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kind", help="filter by kind, e.g. disposicion, ley, decreto, anexo")
    parser.add_argument("--year", type=_year, help="filter by disposition or source route year")
    parser.add_argument("--text", "--q", dest="text", help="free text filter over titles and source text")


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _response_limit(value: Any, *, cursor_payload: Mapping[str, Any], default_limit: int | None) -> int | None:
    if value is not None:
        return int(value)
    if cursor_payload.get("limit") is not None:
        return int(cursor_payload["limit"])
    return default_limit


def _href_url(page_url: str, href: str | None) -> str | None:
    raw = clean_text(href)
    if raw is None:
        return None
    if raw.startswith("#") or raw == "":
        return None
    return absolute_url(page_url, raw)


def _anchor_label(anchor: HtmlNode, *, url: str) -> str | None:
    for value in (anchor.text(), anchor.get("title"), anchor.get("aria-label")):
        label = _optional_text(value)
        if label:
            return label
    return None


def _nearest_context_text(anchor: HtmlNode) -> str | None:
    current: HtmlNode | None = anchor
    while current is not None:
        if current.tag in {"p", "li", "td", "tr"}:
            text = current.text()
            if text:
                return text
        current = current.parent
    return anchor.text()


def _is_legislation_link(*, label: str, context: str | None, target_type: str) -> bool:
    if target_type not in {"normas_pba_detail", "normas_pba_search", "dppj_pdf", "dppj_file", "infoleg", "external_google_drive"}:
        return False
    combined = _search_key(" ".join(part for part in (label, context or "") if part))
    return any(
        token in combined
        for token in (
            "disposicion",
            "ley",
            "decreto",
            "sociedades",
            "asociaciones civiles",
            "formulario",
            "anexo",
        )
    )


def _document_type(
    *,
    label: str,
    context: str | None,
    route: normas_pba.DetailRoute | None,
) -> str:
    combined = _search_key(" ".join(part for part in (label, context or "") if part))
    if "anexo" in combined:
        return "annex"
    if "formulario" in combined:
        return "form"
    if "disposicion" in combined:
        return "disposition"
    if route is not None:
        if route.type_slug == "decreto-ley":
            return "decree-law"
        if route.type_slug == "decreto":
            return "decree"
        if route.type_slug == "ley":
            return "law"
        return route.type_slug
    if "decreto ley" in combined:
        return "decree-law"
    if "decreto" in combined:
        return "decree"
    if "ley" in combined:
        return "law"
    return "document"


def _link_kind(*, url: str, page_url: str, document_type: str, target_type: str) -> str:
    if target_type == "dppj_pdf":
        return "pdf"
    if target_type == "dppj_file":
        return classify_link(url, base_url=page_url)
    if document_type == "annex":
        return "annex"
    return classify_link(url, base_url=page_url)


def _disposition_ref(
    *,
    label: str,
    context: str | None,
    url: str,
    route: normas_pba.DetailRoute | None,
) -> JsonDict:
    combined = " ".join(part for part in (label, context or "") if part)
    match = _DISPOSITION_DIRECT_RE.search(combined) or _DISPOSITION_LATER_RE.search(combined)
    if match:
        return {"number": _normalize_number(match.group("number")), "year": str(_full_year(match.group("year")))}
    file_match = _DISPOSITION_FILE_RE.search(url)
    if file_match:
        number = file_match.group("number") or file_match.group("number2")
        year = file_match.group("year") or file_match.group("year2")
        return {"number": _normalize_number(number), "year": str(_full_year(year))}
    if route is not None and route.type_slug == "disposicion":
        return {"number": _normalize_number(route.number), "year": route.year}
    return {}


def _item_id(
    *,
    label: str,
    url: str,
    target_type: str,
    document_type: str,
    route: normas_pba.DetailRoute | None,
    number: Any,
    year: Any,
    index: int,
    seen_ids: set[str],
) -> str:
    number_text = _optional_text(number)
    year_text = _optional_text(year)
    if document_type in {"disposition", "annex"} and number_text and year_text:
        base = _disposition_id(number_text, year_text)
        if document_type == "annex":
            base = f"{base}:{_slug(label) or 'annex'}"
    elif route is not None:
        base = f"{SOURCE_ID}:normas-pba:{route.type_slug}:{route.year}:{route.number}:{route.internal_id}"
    elif target_type == "infoleg" and _infoleg_id(url):
        base = f"{SOURCE_ID}:infoleg:{_infoleg_id(url)}"
    else:
        base = f"{SOURCE_ID}:official:{target_type}:{_slug(label) or index}"
    item_id = base
    counter = 2
    while item_id in seen_ids:
        item_id = f"{base}:{counter}"
        counter += 1
    seen_ids.add(item_id)
    return item_id


def _preferred_official_match(items: list[LegalItem]) -> LegalItem | None:
    if not items:
        return None
    order = {"normas_pba_detail": 0, "normas_pba_search": 1, "dppj_pdf": 2}
    return sorted(items, key=lambda item: order.get(_optional_text(item.source_fields.get("target_type")) or "", 10))[0]


def _route_from_item(item: LegalItem | None) -> normas_pba.DetailRoute | None:
    if item is None:
        return None
    route = normas_pba.parse_detail_route(item.url)
    if route is not None:
        return route
    source_route = item.source_fields.get("route") or item.source_fields.get("normas_pba_route")
    if isinstance(source_route, Mapping):
        return normas_pba.parse_detail_route(_optional_text(source_route.get("path")) or _optional_text(source_route.get("url")))
    return None


def _official_file(item: LegalItem | None) -> JsonDict | None:
    if item is None or item.file_url is None:
        return None
    return _compact(
        {
            "url": item.file_url,
            "label": item.title,
            "kind": item.source_fields.get("kind") or classify_link(item.file_url),
            "target_type": item.source_fields.get("target_type"),
        }
    )


def _official_links(items: list[LegalItem]) -> list[JsonDict]:
    return [
        _compact(
            {
                "url": item.url,
                "label": item.title,
                "kind": item.source_fields.get("kind") or classify_link(item.url),
                "target_type": item.source_fields.get("target_type"),
                "id": item.id,
            }
        )
        for item in items
        if item.url
    ]


def _normas_summary(document: LegalDocument | None) -> JsonDict | None:
    if document is None:
        return None
    return _compact(
        {
            "id": document.id,
            "title": document.title,
            "date": document.date,
            "url": document.url,
            "metadata": document.metadata,
            "source_fields": document.source_fields,
        }
    )


def _normas_search_fields(url: str) -> JsonDict:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return {
        key: values[0]
        for key, values in params.items()
        if values and key.startswith("q[")
    }


def _infoleg_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = _INFOLEG_ID_RE.search(parsed.path)
    if match:
        return match.group("id")
    return None


def _item_search_parts(item: LegalItem) -> list[str]:
    return [
        item.title or "",
        item.snippet or "",
        str(item.document_type or ""),
        str(item.source_fields.get("source_title") or ""),
        str(item.source_fields.get("source_text") or ""),
        str(item.source_fields.get("target_type") or ""),
    ]


def _kind_matches(item: LegalItem, kind: str) -> bool:
    wanted = _kind_arg(kind)
    values = {
        _kind_arg(item.document_type),
        _kind_arg(item.source_fields.get("kind")),
        _kind_arg(item.source_fields.get("target_type")),
    }
    if wanted == "pdf":
        values.add("pdf" if item.file_url else "")
    return wanted in values


def _item_kind(item: LegalItem) -> str | None:
    return _kind_arg(item.document_type)


def _item_number(item: LegalItem) -> str | None:
    return _normalize_number(item.source_fields.get("number") or item.facets.get("number"))


def _item_year(item: LegalItem) -> str | None:
    year = item.source_fields.get("year") or item.facets.get("year")
    return _optional_text(year)


def _kind_arg(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    key = _search_key(text)
    normalized = KIND_ALIASES.get(key, key.replace(" ", "-"))
    return normalized


def _required_number(value: Any) -> str:
    number = _normalize_number(value)
    if number is None:
        raise usage_error("--number is required")
    return number


def _required_year(value: Any) -> str:
    if value is None:
        raise usage_error("--year is required")
    return str(value)


def _normalize_number(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    return str(int(digits)) if digits else None


def _disposition_id(number: str, year: str) -> str:
    return f"{SOURCE_ID}:disposition:{year}:{_normalize_number(number) or number}"


def _year(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1800 or parsed > 2200:
        raise argparse.ArgumentTypeError("must be between 1800 and 2200")
    return parsed


def _full_year(value: str) -> int:
    year = int(value)
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _slug(value: Any) -> str:
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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {str(key): item for key, item in value.items() if item not in (None, "", [])}


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


def _dedupe_texts(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _optional_text(value)
        if text is None or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Referer": BASE_URL})


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
        source_urls=[LEGISLACION_URL, normas_pba.SEARCH_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


register_adapter(build_adapter(), replace=True)
