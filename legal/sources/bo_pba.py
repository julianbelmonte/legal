"""Boletin Oficial PBA direct search adapter."""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from email.message import Message
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from legal.errors import LegalCliError, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.parsing import HtmlNode, absolute_url, clean_snippet, clean_text, normalize_date, parse_html
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "bo-pba"
SOURCE_MAP = "legal/docs/boletin_oficial_pba.md"

BASE_URL = "https://boletinoficial.gba.gob.ar"
SEARCH_URL = f"{BASE_URL}/buscar"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 500
AJAX_ACCEPT = "application/json, text/javascript, */*; q=0.01"
SECTIONS = {"OFICIAL", "JUDICIAL", "JURISPRUDENCIA", "SUPLEMENTO"}
SORT_ALIASES: Mapping[str, str] = {
    "match": "by_match_desc",
    "match_desc": "by_match_desc",
    "by_match_desc": "by_match_desc",
    "date_desc": "by_date_desc",
    "newest": "by_date_desc",
    "recent": "by_date_desc",
    "by_date_desc": "by_date_desc",
    "date_asc": "by_date_asc",
    "oldest": "by_date_asc",
    "by_date_asc": "by_date_asc",
}
SORT_VALUES = set(SORT_ALIASES.values())

_BULLETIN_RE = re.compile(
    r"N[°º]?\s*(?P<number>\d+)\s*-\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
_SECTION_ROUTE_RE = re.compile(r"/secciones/(?P<section_id>\d+)/(?P<action>ver|descargar)(?:[#?].*)?$")
_PAGE_RE = re.compile(r"(?:page=|p[aá]gina\s*)(?P<page>\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class SectionSearchMeta:
    section: str
    section_id: str | None
    date: str | None
    pdf_view_url: str | None
    pdf_download_url: str | None
    ajax_url: str | None
    ajax_total: int | None
    first_page_count: int

    def to_dict(self) -> JsonDict:
        return _compact(
            {
                "section": self.section,
                "section_id": self.section_id,
                "date": self.date,
                "pdf_view_url": self.pdf_view_url,
                "pdf_download_url": self.pdf_download_url,
                "ajax_url": self.ajax_url,
                "ajax_total": self.ajax_total,
                "first_page_count": self.first_page_count,
            }
        )


@dataclass(frozen=True)
class BoPbaSearchPage:
    url: str
    html: str
    items: list[LegalItem]
    bulletin_number: str | None
    bulletin_date: str | None
    sections: list[SectionSearchMeta]
    total: int | None
    headers: JsonDict


@dataclass(frozen=True)
class BoPbaAjaxPage:
    url: str
    items: list[LegalItem]
    html_fragments: list[str]
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", help="publication date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="publication date upper bound, YYYY-MM-DD")
    parser.add_argument("--section", help="section: OFICIAL, JUDICIAL, JURISPRUDENCIA, or SUPLEMENTO")
    parser.add_argument("--words", "--text", "--q", dest="words", help="word or phrase search")
    parser.add_argument("--sort", help="sort alias or raw value: by_match_desc, by_date_desc, by_date_asc")


def add_pages_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="bulletin date, YYYY-MM-DD")
    parser.add_argument("--section", help="section: OFICIAL, JUDICIAL, JURISPRUDENCIA, or SUPLEMENTO")
    parser.add_argument("--query", default="", help="search query used by the AJAX result page")
    parser.add_argument("--page", type=int, help="AJAX result page number")


def add_section_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="section_id", help="Boletin Oficial PBA section id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", default=None, help="inspect the attachment PDF endpoint")
    mode.add_argument("--view", action="store_true", default=None, help="inspect the inline PDF endpoint")


def handle_search(args: argparse.Namespace) -> LegalResponse:
    query = _query_from_args(args)
    limit = int(args.limit or DEFAULT_LIMIT)

    with _make_client() as client:
        search_page = fetch_search_page(query=query, client=client, include_raw=bool(args.raw))

    items = search_page.items[:limit]
    total = search_page.total if search_page.total is not None else len(search_page.items)
    page_info = PageInfo(
        limit=limit,
        offset=0,
        page=1,
        total=total,
        has_more=len(items) < total,
    )

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "offset": 0, "limit": limit},
        items=items,
        page=page_info,
        provenance=_provenance(
            fetched_urls=[search_page.url],
            source_response_id=_bulletin_response_id(search_page),
            raw={
                "headers": search_page.headers,
                "bulletin_number": search_page.bulletin_number,
                "bulletin_date": search_page.bulletin_date,
                "sections": [section.to_dict() for section in search_page.sections],
            },
        ),
        facets={
            "sections": sorted(SECTIONS),
            "sorts": sorted(SORT_VALUES),
        },
    )


def handle_pages(args: argparse.Namespace) -> LegalResponse:
    requested_date = _iso_date_arg(args.date, flag="--date")
    if requested_date is None:
        raise usage_error("--date is required")
    section = _section_arg(args.section)
    if section is None:
        raise usage_error("--section is required")
    page_number = _positive_page_arg(args.page, flag="--page")
    query_text = _optional_text(args.query) or ""
    limit = int(args.limit) if args.limit else None

    with _make_client() as client:
        ajax_page = fetch_ajax_page(
            bulletin_date=requested_date,
            section=section,
            query=query_text,
            page=page_number,
            client=client,
            include_raw=bool(args.raw),
        )

    items = ajax_page.items[:limit] if limit is not None else ajax_page.items
    operation = getattr(args, "operation", "pages")
    response_query: JsonDict = {
        "date": requested_date,
        "section": section,
        "query": query_text,
        "page": page_number,
    }
    if limit is not None:
        response_query["limit"] = limit

    return LegalResponse.search(
        source=SOURCE_ID,
        operation=operation,
        query=response_query,
        items=items,
        page=PageInfo(
            limit=limit if limit is not None else len(items),
            offset=0,
            page=page_number,
            total=len(ajax_page.items),
            has_more=len(items) < len(ajax_page.items),
        ),
        provenance=_provenance(
            fetched_urls=[ajax_page.url],
            source_response_id=f"{SOURCE_ID}:{requested_date}:{section.lower()}:page-{page_number}",
            raw={
                "headers": ajax_page.headers,
                "html_fragment_count": len(ajax_page.html_fragments),
                "item_count": len(ajax_page.items),
            },
        ),
    )


def handle_section(args: argparse.Namespace) -> LegalResponse:
    section_id = _section_id_arg(args.section_id)
    if not args.download and not args.view:
        raise usage_error("one of --download or --view is required")
    action = "descargar" if args.download else "ver"
    mode = "download" if args.download else "view"

    with _make_client() as client:
        response = fetch_section_metadata(section_id=section_id, action=action, client=client)

    operation = getattr(args, "operation", "section")
    document = section_response_to_document(
        section_id=section_id,
        action=action,
        mode=mode,
        response=response,
        include_raw=bool(args.raw),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation=operation,
        request={"id": section_id, "mode": mode, "action": action},
        document=document,
        provenance=document.provenance,
    )


def fetch_search_page(
    *,
    query: Mapping[str, Any],
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> BoPbaSearchPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", SEARCH_URL, params=search_params(query))
        return parse_search_response(response, include_raw=include_raw)
    finally:
        if owns_client:
            http.close()


def fetch_ajax_page(
    *,
    bulletin_date: str,
    section: str,
    query: str,
    page: int,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> BoPbaAjaxPage:
    owns_client = client is None
    http = client or _make_client()
    url = f"{BASE_URL}/boletin/{bulletin_date}/paginas/{section}"
    try:
        response = http.request(
            "GET",
            url,
            params={"q": query, "page": page},
            headers={"Accept": AJAX_ACCEPT, "X-Requested-With": "XMLHttpRequest"},
        )
        return parse_ajax_response(
            response,
            bulletin_date=bulletin_date,
            section=section,
            query=query,
            page=page,
            include_raw=include_raw,
        )
    finally:
        if owns_client:
            http.close()


def fetch_section_metadata(
    *,
    section_id: str,
    action: str,
    client: LegalHttpClient | None = None,
) -> httpx.Response:
    owns_client = client is None
    http = client or _make_client()
    url = _section_url(section_id=section_id, action=action)
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


def parse_search_response(response: httpx.Response, *, include_raw: bool = False) -> BoPbaSearchPage:
    html = response.text
    page_url = str(response.url)
    root = parse_html(html)
    bulletin_number, bulletin_date = parse_bulletin_metadata(root)
    items: list[LegalItem] = []
    sections: list[SectionSearchMeta] = []
    seen: set[str] = set()

    for result_box in _iter_by_class(root, "result-box"):
        section_meta, section_items = _parse_result_box(
            result_box,
            page_url=page_url,
            bulletin_number=bulletin_number,
            bulletin_date=bulletin_date,
            include_raw=include_raw,
        )
        if section_meta is None:
            continue
        sections.append(section_meta)
        for item in section_items:
            if item.id in seen:
                continue
            seen.add(item.id)
            items.append(item)

    total = _search_total(sections=sections, items=items)
    return BoPbaSearchPage(
        url=page_url,
        html=html,
        items=items,
        bulletin_number=bulletin_number,
        bulletin_date=bulletin_date,
        sections=sections,
        total=total,
        headers=_useful_headers(response),
    )


def parse_ajax_response(
    response: httpx.Response,
    *,
    bulletin_date: str,
    section: str,
    query: str,
    page: int,
    include_raw: bool = False,
) -> BoPbaAjaxPage:
    try:
        payload = response.json()
    except ValueError as exc:
        raise parse_error(
            "Boletin Oficial PBA AJAX response was not valid JSON",
            details=_response_evidence(response, include_body=True),
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        ) from exc

    fragments = _ajax_html_fragments(payload, response=response)
    items = _ajax_items(
        fragments=fragments,
        page_url=str(response.url),
        bulletin_date=bulletin_date,
        section=section,
        query=query,
        page=page,
        include_raw=include_raw,
    )
    return BoPbaAjaxPage(
        url=str(response.url),
        items=items,
        html_fragments=fragments,
        headers=_useful_headers(response),
    )


def section_response_to_document(
    *,
    section_id: str,
    action: str,
    mode: str,
    response: httpx.Response,
    include_raw: bool = False,
) -> LegalDocument:
    url = _public_url(str(response.url)) or _section_url(section_id=section_id, action=action)
    content_type = _optional_text(response.headers.get("content-type"))
    content_length = _content_length(response.headers.get("content-length"))
    content_disposition = _optional_text(response.headers.get("content-disposition"))
    disposition_type, filename = _content_disposition_parts(content_disposition)
    kind = "pdf" if _is_pdf(content_type=content_type, filename=filename, url=url) else "file"
    document_id = f"{SOURCE_ID}:section:{section_id}:{action}"
    file_entry = _compact(
        {
            "url": url,
            "label": filename or f"seccion-{section_id}.pdf",
            "kind": kind,
            "content_type": content_type,
            "content_length": content_length,
            "content_disposition": content_disposition,
            "disposition_type": disposition_type,
            "mode": mode,
        }
    )
    metadata = _compact(
        {
            "section_id": section_id,
            "action": action,
            "mode": mode,
            "filename": filename,
            "content_type": content_type,
            "content_length": content_length,
            "content_disposition": content_disposition,
            "disposition_type": disposition_type,
            "last_modified": _optional_text(response.headers.get("last-modified")),
            "etag": _optional_text(response.headers.get("etag")),
            "method": response.request.method,
            "status_code": response.status_code,
            "inline": disposition_type == "inline",
            "attachment": disposition_type == "attachment",
        }
    )
    return LegalDocument(
        id=document_id,
        title=filename or f"Boletin Oficial PBA section {section_id} {mode}",
        document_type=kind,
        url=url,
        file_url=url,
        content_type=content_type,
        metadata=metadata,
        links=[{"url": url, "label": mode, "kind": kind, "mode": mode}],
        files=[file_entry],
        source_fields={"section_id": section_id, "action": action, "mode": mode},
        raw={"headers": _useful_headers(response)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=document_id,
            raw={
                "method": response.request.method,
                "status_code": response.status_code,
                "content_disposition": content_disposition,
                "disposition_type": disposition_type,
            },
        ),
    )


def parse_bulletin_metadata(root: HtmlNode | str) -> tuple[str | None, str | None]:
    parsed = parse_html(root) if isinstance(root, str) else root
    for node in _iter_by_class(parsed, "last-bulletin"):
        match = _BULLETIN_RE.search(node.text() or "")
        if match:
            return match.group("number"), normalize_date(match.group("date"))
    match = _BULLETIN_RE.search(parsed.text() or "")
    if match:
        return match.group("number"), normalize_date(match.group("date"))
    return None, None


def search_params(query: Mapping[str, Any]) -> JsonDict:
    return {
        "search[date_gteq]": _http_date(query.get("from")),
        "search[date_lteq]": _http_date(query.get("to")),
        "search[section]": query.get("section") or "",
        "search[words]": query.get("words") or "",
        "search[sort]": query.get("sort") or "",
        "commit": "Buscar",
    }


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("Boletin Oficial PBA source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("search", handle_search, help="search Boletin Oficial PBA", add_arguments=add_search_arguments)
    adapter.register_operation("pages", handle_pages, help="fetch Boletin Oficial PBA AJAX result pages", add_arguments=add_pages_arguments)
    adapter.register_operation("section", handle_section, help="inspect Boletin Oficial PBA section PDF metadata", add_arguments=add_section_arguments)
    adapter.register_operation("next", handle_pages, help="alias for Boletin Oficial PBA AJAX result pages", add_arguments=add_pages_arguments)
    adapter.register_operation("pdf", handle_section, help="alias for Boletin Oficial PBA section PDF metadata", add_arguments=add_section_arguments)
    adapter.register_operation("get", handle_section, help="alias for Boletin Oficial PBA section PDF metadata", add_arguments=add_section_arguments)
    return adapter


def _parse_result_box(
    result_box: HtmlNode,
    *,
    page_url: str,
    bulletin_number: str | None,
    bulletin_date: str | None,
    include_raw: bool,
) -> tuple[SectionSearchMeta | None, list[LegalItem]]:
    title_node = _first_by_class(result_box, "title")
    section = _section_label(title_node)
    if section is None:
        return None, []

    section_id, pdf_view_url, pdf_download_url = _section_links(title_node, page_url=page_url)
    publication_date = _result_box_date(result_box) or bulletin_date
    ajax_url, ajax_total = _ajax_metadata(result_box, page_url=page_url)
    items = _result_items(
        result_box,
        page_url=page_url,
        section=section,
        section_id=section_id,
        publication_date=publication_date,
        bulletin_number=bulletin_number,
        bulletin_date=bulletin_date,
        pdf_view_url=pdf_view_url,
        pdf_download_url=pdf_download_url,
        ajax_url=ajax_url,
        ajax_total=ajax_total,
        include_raw=include_raw,
    )
    section_meta = SectionSearchMeta(
        section=section,
        section_id=section_id,
        date=publication_date,
        pdf_view_url=pdf_view_url,
        pdf_download_url=pdf_download_url,
        ajax_url=ajax_url,
        ajax_total=ajax_total,
        first_page_count=len(items),
    )
    return section_meta, items


def _result_items(
    result_box: HtmlNode,
    *,
    page_url: str,
    section: str,
    section_id: str | None,
    publication_date: str | None,
    bulletin_number: str | None,
    bulletin_date: str | None,
    pdf_view_url: str | None,
    pdf_download_url: str | None,
    ajax_url: str | None,
    ajax_total: int | None,
    include_raw: bool,
) -> list[LegalItem]:
    items: list[LegalItem] = []
    result_nodes = _iter_by_class(result_box, "ajax-result")
    for result_node in result_nodes:
        for section_index, (anchor, excerpt) in enumerate(_excerpt_pairs(result_node), start=len(items) + 1):
            item = _excerpt_item(
                anchor=anchor,
                excerpt=excerpt,
                page_url=page_url,
                section=section,
                section_id=section_id,
                section_index=section_index,
                publication_date=publication_date,
                bulletin_number=bulletin_number,
                bulletin_date=bulletin_date,
                pdf_view_url=pdf_view_url,
                pdf_download_url=pdf_download_url,
                ajax_url=ajax_url,
                ajax_total=ajax_total,
                include_raw=include_raw,
            )
            if item is not None:
                items.append(item)
    return items


def _excerpt_item(
    *,
    anchor: HtmlNode | None,
    excerpt: HtmlNode,
    page_url: str,
    section: str,
    section_id: str | None,
    section_index: int,
    publication_date: str | None,
    bulletin_number: str | None,
    bulletin_date: str | None,
    pdf_view_url: str | None,
    pdf_download_url: str | None,
    ajax_url: str | None,
    ajax_total: int | None,
    include_raw: bool,
) -> LegalItem | None:
    result_url = absolute_url(page_url, anchor.get("href") if anchor is not None else None) if anchor is not None else None
    page_number = _page_number(anchor=anchor, url=result_url)
    snippet = clean_snippet(excerpt, max_length=SNIPPET_LENGTH)
    if result_url is None and snippet is None:
        return None

    resolved_section_id = section_id or _section_id_from_url(result_url)
    resolved_pdf_view_url = pdf_view_url or (
        f"{BASE_URL}/secciones/{resolved_section_id}/ver" if resolved_section_id is not None else None
    )
    resolved_pdf_download_url = pdf_download_url or (
        f"{BASE_URL}/secciones/{resolved_section_id}/descargar" if resolved_section_id is not None else None
    )
    item_id = _item_id(
        bulletin_date=bulletin_date or publication_date,
        section=section,
        section_id=resolved_section_id,
        page_number=page_number,
        index=section_index,
    )
    title = " ".join(
        part
        for part in (
            "Boletin Oficial PBA",
            bulletin_number,
            section,
            f"pagina {page_number}" if page_number is not None else None,
        )
        if part
    )
    highlights = _highlights(excerpt)
    return LegalItem(
        id=item_id,
        title=title,
        date=publication_date,
        document_type="bulletin_excerpt",
        url=result_url or resolved_pdf_view_url,
        file_url=resolved_pdf_download_url,
        snippet=snippet,
        facets=_compact(
            {
                "section": section,
                "section_id": resolved_section_id,
                "bulletin_number": bulletin_number,
                "bulletin_date": bulletin_date,
                "page": page_number,
            }
        ),
        source_fields=_compact(
            {
                "bulletin_number": bulletin_number,
                "bulletin_date": bulletin_date,
                "publication_date": publication_date,
                "section": section,
                "section_id": resolved_section_id,
                "page_number": page_number,
                "result_url": result_url,
                "pdf_view_url": resolved_pdf_view_url,
                "pdf_download_url": resolved_pdf_download_url,
                "highlights": highlights,
                "ajax": _compact({"url": ajax_url, "total": ajax_total}),
                "section_index": section_index,
            }
        ),
        raw={"anchor": anchor.text() if anchor is not None else None, "excerpt_html": _node_htmlish(excerpt)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=item_id,
            raw={"section": section, "section_id": resolved_section_id, "page": page_number},
        ),
    )


def _query_from_args(args: argparse.Namespace) -> JsonDict:
    query: JsonDict = {
        "from": _iso_date_arg(args.date_from, flag="--from"),
        "to": _iso_date_arg(args.date_to, flag="--to"),
        "section": _section_arg(args.section),
        "words": _optional_text(args.words),
        "sort": _sort_arg(args.sort),
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _ajax_html_fragments(payload: Any, *, response: httpx.Response) -> list[str]:
    if not isinstance(payload, Mapping):
        raise parse_error(
            "Boletin Oficial PBA AJAX response was not an object",
            details={"url": str(response.url), "shape": type(payload).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
        )
    html = payload.get("html")
    if html is None:
        return []
    if isinstance(html, str):
        return [html]
    if isinstance(html, list) and all(isinstance(fragment, str) for fragment in html):
        return list(html)
    raise parse_error(
        "Boletin Oficial PBA AJAX html field had an unexpected shape",
        details={"url": str(response.url), "shape": type(html).__name__},
        provenance=_provenance(fetched_urls=[str(response.url)], raw=_response_evidence(response)),
    )


def _ajax_items(
    *,
    fragments: list[str],
    page_url: str,
    bulletin_date: str,
    section: str,
    query: str,
    page: int,
    include_raw: bool,
) -> list[LegalItem]:
    items: list[LegalItem] = []
    ajax_url = _public_url(page_url)
    for fragment in fragments:
        root = parse_html(fragment)
        result_nodes = _iter_by_class(root, "ajax-result")
        if not result_nodes and _excerpt_pairs(root):
            result_nodes = [root]
        for result_node in result_nodes:
            for section_index, (anchor, excerpt) in enumerate(_excerpt_pairs(result_node), start=len(items) + 1):
                item = _excerpt_item(
                    anchor=anchor,
                    excerpt=excerpt,
                    page_url=page_url,
                    section=section,
                    section_id=None,
                    section_index=section_index,
                    publication_date=bulletin_date,
                    bulletin_number=None,
                    bulletin_date=bulletin_date,
                    pdf_view_url=None,
                    pdf_download_url=None,
                    ajax_url=ajax_url,
                    ajax_total=None,
                    include_raw=include_raw,
                )
                if item is not None:
                    item.source_fields["ajax"].update({"query": query, "page": page})
                    items.append(item)
    return items


def _section_label(title_node: HtmlNode | None) -> str | None:
    if title_node is None:
        return None
    for anchor in title_node.iter("a"):
        text = _optional_text(anchor.text())
        if text and text.upper() in SECTIONS:
            return text.upper()
    text = _optional_text(title_node.text())
    if text is None:
        return None
    for section in SECTIONS:
        if section in text.upper():
            return section
    return None


def _section_links(title_node: HtmlNode | None, *, page_url: str) -> tuple[str | None, str | None, str | None]:
    section_id: str | None = None
    pdf_view_url: str | None = None
    pdf_download_url: str | None = None
    if title_node is None:
        return section_id, pdf_view_url, pdf_download_url

    for anchor in title_node.iter("a"):
        url = absolute_url(page_url, anchor.get("href"))
        if url is None:
            continue
        parsed = urlparse(url)
        match = _SECTION_ROUTE_RE.search(parsed.path)
        if not match:
            continue
        section_id = section_id or match.group("section_id")
        if match.group("action") == "ver":
            pdf_view_url = _public_url(url)
        elif match.group("action") == "descargar":
            pdf_download_url = _public_url(url)
    if pdf_view_url is None and section_id is not None:
        pdf_view_url = f"{BASE_URL}/secciones/{section_id}/ver"
    if pdf_download_url is None and section_id is not None:
        pdf_download_url = f"{BASE_URL}/secciones/{section_id}/descargar"
    return section_id, pdf_view_url, pdf_download_url


def _result_box_date(result_box: HtmlNode) -> str | None:
    for node in _iter_by_class(result_box, "date"):
        normalized = normalize_date(node.text())
        if normalized:
            return normalized
    return None


def _ajax_metadata(result_box: HtmlNode, *, page_url: str) -> tuple[str | None, int | None]:
    for paginator in _iter_by_class(result_box, "ajax-paginator"):
        ajax_url = absolute_url(page_url, paginator.get("data-link"))
        return _public_url(ajax_url) if ajax_url else None, _parse_int(paginator.get("data-total"))
    return None, None


def _excerpt_pairs(result_node: HtmlNode) -> list[tuple[HtmlNode | None, HtmlNode]]:
    pairs: list[tuple[HtmlNode | None, HtmlNode]] = []
    current_anchor: HtmlNode | None = None
    for child in result_node.children:
        if not isinstance(child, HtmlNode):
            continue
        if child.tag == "a" and _has_class(child, "page"):
            current_anchor = child
            continue
        if child.tag == "p" and _has_class(child, "excerpt"):
            pairs.append((current_anchor, child))
            current_anchor = None
    return pairs


def _page_number(*, anchor: HtmlNode | None, url: str | None) -> int | None:
    for value in (url, anchor.text() if anchor is not None else None):
        text = _optional_text(value)
        if not text:
            continue
        match = _PAGE_RE.search(text)
        if match:
            return _parse_int(match.group("page"))
    return None


def _section_id_from_url(url: str | None) -> str | None:
    text = _optional_text(url)
    if text is None:
        return None
    match = _SECTION_ROUTE_RE.search(urlparse(text).path)
    return match.group("section_id") if match else None


def _item_id(
    *,
    bulletin_date: str | None,
    section: str,
    section_id: str | None,
    page_number: int | None,
    index: int,
) -> str:
    date_part = bulletin_date or "unknown-date"
    section_part = section.lower()
    id_part = section_id or "unknown-section"
    page_part = f"page-{page_number}" if page_number is not None else f"hit-{index}"
    return f"{SOURCE_ID}:{date_part}:{section_part}:{id_part}:{page_part}:{index}"


def _highlights(excerpt: HtmlNode) -> list[str]:
    highlights: list[str] = []
    seen: set[str] = set()
    for node in excerpt.iter("span"):
        if not _has_class(node, "result-highlight"):
            continue
        text = _optional_text(node.text())
        if text is None or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        highlights.append(text)
    return highlights


def _search_total(*, sections: list[SectionSearchMeta], items: list[LegalItem]) -> int | None:
    totals = [section.ajax_total for section in sections if section.ajax_total is not None]
    if totals:
        return sum(totals)
    return len(items) if items else 0


def _bulletin_response_id(search_page: BoPbaSearchPage) -> str | None:
    if search_page.bulletin_date and search_page.bulletin_number:
        return f"{SOURCE_ID}:{search_page.bulletin_date}:{search_page.bulletin_number}"
    if search_page.bulletin_date:
        return f"{SOURCE_ID}:{search_page.bulletin_date}"
    return None


def _iso_date_arg(value: Any, *, flag: str) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise usage_error(f"{flag} must be an ISO date YYYY-MM-DD") from exc
    return text


def _positive_page_arg(value: Any, *, flag: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise usage_error(f"{flag} must be an integer") from exc
    if parsed < 1:
        raise usage_error(f"{flag} must be greater than or equal to 1")
    return parsed


def _section_id_arg(value: Any) -> str:
    text = _optional_text(value)
    if text is None or not text.isdigit():
        raise usage_error("--id must be a numeric section id", details={"id": value})
    return text


def _http_date(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return ""
    parsed = date.fromisoformat(text)
    return f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}"


def _section_arg(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    section = text.upper()
    if section not in SECTIONS:
        raise usage_error("unknown Boletin Oficial PBA section", details={"section": text, "known_sections": sorted(SECTIONS)})
    return section


def _sort_arg(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    sort = SORT_ALIASES.get(text.strip().lower().replace("-", "_"))
    if sort is None:
        raise usage_error("unknown Boletin Oficial PBA sort", details={"sort": text, "known_sorts": sorted(SORT_VALUES)})
    return sort


def _section_url(*, section_id: str, action: str) -> str:
    if action not in {"ver", "descargar"}:
        raise usage_error("unknown Boletin Oficial PBA section PDF action", details={"action": action})
    return f"{BASE_URL}/secciones/{section_id}/{action}"


def _iter_by_class(root: HtmlNode, class_name: str) -> list[HtmlNode]:
    return [node for node in root.iter() if _has_class(node, class_name)]


def _first_by_class(root: HtmlNode, class_name: str) -> HtmlNode | None:
    return next((node for node in root.iter() if _has_class(node, class_name)), None)


def _has_class(node: HtmlNode, class_name: str) -> bool:
    return class_name in (node.get("class") or "").split()


def _parse_int(value: Any) -> int | None:
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
    return {str(key): item for key, item in value.items() if item not in (None, "", [])}


def _content_length(value: Any) -> int | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _content_disposition_parts(value: str | None) -> tuple[str | None, str | None]:
    text = _optional_text(value)
    if text is None:
        return None, None
    message = Message()
    message["content-disposition"] = text
    return message.get_content_disposition(), _optional_text(message.get_filename())


def _is_pdf(*, content_type: str | None, filename: str | None, url: str | None) -> bool:
    if content_type and content_type.split(";", 1)[0].strip().lower() == "application/pdf":
        return True
    for value in (filename, url):
        text = _optional_text(value)
        if text and urlparse(text).path.lower().endswith(".pdf"):
            return True
    return False


def _node_htmlish(node: HtmlNode) -> str:
    return node.text() or ""


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Referer": SEARCH_URL})


def _useful_headers(response: httpx.Response) -> JsonDict:
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    is_file_response = content_type == "application/pdf" or "content-disposition" in response.headers
    allowed = {"content-type", "etag", "last-modified"}
    if is_file_response:
        allowed.update({"content-disposition", "content-length"})
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


def _status_code(error: LegalCliError) -> int | None:
    details = error.details or {}
    status = details.get("status_code")
    return status if isinstance(status, int) else None


def _provenance(
    *,
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=[SEARCH_URL],
        fetched_urls=[_public_url(url) for url in fetched_urls],
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _public_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() != "boletinoficial.gba.gob.ar":
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, parsed.fragment))


register_adapter(build_adapter(), replace=True)
