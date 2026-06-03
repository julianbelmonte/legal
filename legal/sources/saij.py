"""SAIJ search/detail adapter."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from legal import enrichment
from legal.errors import not_found, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import LegalDocument, LegalItem, LegalResponse, Provenance
from legal.pagination import build_page_info, decode_cursor
from legal.parsing import absolute_url, classify_link, clean_snippet, clean_text, extract_links, normalize_date
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "saij"
SOURCE_MAP = "legal/docs/saij_jurisprudencia.md"
PUBLIC_BASE_URL = "https://www.saij.gob.ar"
HUMAN_URL = f"{PUBLIC_BASE_URL}/"
SEARCH_URL = f"{PUBLIC_BASE_URL}/busqueda"
DOCUMENT_DISPLAY_URL = f"{PUBLIC_BASE_URL}/documentDisplay.jsp"
VIEW_DOCUMENT_URL = f"{PUBLIC_BASE_URL}/view-document"

DEFAULT_LIMIT = 10
DEFAULT_OFFSET = 0
DEFAULT_VIEW = "colapsada"
SNIPPET_LENGTH = 320

DEFAULT_FACETS = (
    "Total|Tipo de Documento|Fecha|Organismo|Tribunal|Publicación|Tema|"
    "Estado de Vigencia|Autor|Jurisdicción"
)

TYPE_FACETS = {
    "fallo": "Tipo de Documento/Jurisprudencia",
    "fallos": "Tipo de Documento/Jurisprudencia",
    "jurisprudencia": "Tipo de Documento/Jurisprudencia",
    "sumario": "Tipo de Documento/Jurisprudencia/Sumario",
    "sumarios": "Tipo de Documento/Jurisprudencia/Sumario",
    "legislacion": "Tipo de Documento/Legislación",
    "legislación": "Tipo de Documento/Legislación",
    "norma": "Tipo de Documento/Legislación",
    "normativa": "Tipo de Documento/Legislación",
    "dictamen": "Tipo de Documento/Dictamen",
    "dictamenes": "Tipo de Documento/Dictamen",
    "dictámenes": "Tipo de Documento/Dictamen",
    "doctrina": "Tipo de Documento/Doctrina",
}

JsonDict = dict[str, Any]

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SAIJ_MARKUP_RE = re.compile(r"\[\[/?[a-zA-Z0-9_-]+(?:\s+[^\]]*)?\]\]")
_URL_KEY_RE = re.compile(r"(?:url|uri|href|link|enlace|archivo|adjunto|pdf|doc)", re.IGNORECASE)

ATTACHMENT_KEYS = {
    "adjunto",
    "adjunto_pdf",
    "adjunto-pdf",
    "adjuntopdf",
    "archivo",
    "archivo_pdf",
    "archivo-pdf",
    "archivos",
    "anexo",
    "anexos",
    "pdf",
    "url_pdf",
    "url-pdf",
}


@dataclass(frozen=True)
class SaijSearchPage:
    payload: JsonDict
    hits: list[JsonDict]
    total: int | None
    facets: JsonDict
    query_object: JsonDict
    fetched_url: str
    headers: JsonDict


@dataclass(frozen=True)
class SaijDocumentPage:
    payload: JsonDict
    data_payload: JsonDict
    document: JsonDict
    metadata: JsonDict
    content: JsonDict
    guid: str
    fetched_url: str
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text query mapped to SAIJ texto")
    parser.add_argument("--raw-query", dest="raw_query", help="SAIJ raw query expression passed as r")
    parser.add_argument("--facets", help="pipe-separated SAIJ facets passed as f")
    parser.add_argument("--offset", type=_non_negative_int, help="zero-based SAIJ search offset")
    parser.add_argument("--type", dest="document_type", help="SAIJ document type preset, e.g. fallo or sumario")
    parser.add_argument("--sort", help="SAIJ sort expression passed as s")


def add_facets_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text query mapped to SAIJ texto")
    parser.add_argument("--raw-query", dest="raw_query", help="SAIJ raw query expression passed as r")
    parser.add_argument("--facets", help="pipe-separated SAIJ facets passed as f")
    parser.add_argument("--offset", type=_non_negative_int, help="zero-based SAIJ search offset")
    parser.add_argument("--type", dest="document_type", help="SAIJ document type preset, e.g. fallo or sumario")
    parser.add_argument("--sort", help="SAIJ sort expression passed as s")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--guid",
        "--id",
        dest="guid",
        help="SAIJ document guid from search results (accepts the saij:<guid> item id too)",
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    offset = int(args.offset if args.offset is not None else cursor_payload.get("offset", DEFAULT_OFFSET))

    with _make_client() as client:
        search_page = fetch_search_page(
            raw_query=query["raw_query"],
            facets=query["facets"],
            offset=offset,
            limit=limit,
            sort=query.get("sort"),
            client=client,
        )

    items = [
        hit_to_item(hit, search_page=search_page, include_raw=bool(args.raw))
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
                "iterationToken": _search_result_value(search_page.payload, "iterationToken"),
                "expandedQuery": _search_result_value(search_page.payload, "expandedQuery"),
                "inputQuery": _search_result_value(search_page.payload, "inputQuery"),
            },
        ),
        facets=search_page.facets,
    )


def handle_facets(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="facets")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.limit or cursor_payload.get("limit") or 1)
    offset = int(args.offset if args.offset is not None else cursor_payload.get("offset", DEFAULT_OFFSET))

    with _make_client() as client:
        search_page = fetch_search_page(
            raw_query=query["raw_query"],
            facets=query["facets"],
            offset=offset,
            limit=limit,
            sort=query.get("sort"),
            client=client,
        )

    return LegalResponse(
        ok=True,
        source=SOURCE_ID,
        operation="facets",
        query={**query, "offset": offset, "limit": limit},
        facets=search_page.facets,
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            raw={
                "headers": search_page.headers,
                "queryObjectData": search_page.query_object,
                "iterationToken": _search_result_value(search_page.payload, "iterationToken"),
            },
        ),
        warnings=[],
    )


def _guid_from_args(args: argparse.Namespace) -> str:
    guid = _optional_text(args.guid)
    if guid is None:
        raise usage_error("--guid (or --id) is required")
    # search items expose their id as "saij:<guid>"; accept it verbatim
    if guid.startswith(f"{SOURCE_ID}:"):
        guid = guid.split(":", 1)[1]
    return guid


def handle_get(args: argparse.Namespace) -> LegalResponse:
    guid = _guid_from_args(args)

    with _make_client() as client:
        document_page = fetch_document(guid=guid, client=client)

    document = document_page_to_document(document_page, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={"guid": guid},
        document=document,
        provenance=document.provenance,
    )


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--guid",
        "--id",
        dest="guid",
        help="SAIJ document guid (accepts the saij:<guid> item id too)",
    )
    enrichment.add_text_arguments(parser)


def handle_download(args: argparse.Namespace) -> LegalResponse:
    guid = _guid_from_args(args)

    with _make_client() as client:
        document_page = fetch_document(guid=guid, client=client)
        base_document = document_page_to_document(document_page, include_raw=bool(args.raw))
        pdf_file = next(
            (
                f
                for f in base_document.files
                if f.get("kind") == "pdf" or str(f.get("url", "")).lower().endswith(".pdf")
            ),
            None,
        )
        if pdf_file is None:
            raise not_found(
                "SAIJ document has no downloadable PDF attachment",
                details={"guid": guid, "files": base_document.files},
                provenance=base_document.provenance,
            )
        pdf_url = str(pdf_file["url"])
        response = client.request("GET", pdf_url)
        pdf_bytes = response.content

    want_text = bool(getattr(args, "want_text", False))
    pdf_meta = enrichment.finalize_document(
        pdf_bytes,
        want_text=want_text,
        save_path=_optional_text(getattr(args, "save_pdf", None)),
    )
    document = LegalDocument(
        id=base_document.id,
        title=base_document.title,
        date=base_document.date,
        document_type=base_document.document_type,
        body=pdf_meta.get("text") if want_text else None,
        url=base_document.url,
        file_url=pdf_url,
        content_type="application/pdf",
        text_format="plain_text" if want_text else None,
        metadata={**base_document.metadata, "attachment_label": pdf_file.get("label"), **pdf_meta},
        files=base_document.files,
        source_fields=base_document.source_fields,
        raw=base_document.raw if bool(args.raw) else {},
        provenance=base_document.provenance,
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request={"guid": guid},
        document=document,
        provenance=document.provenance,
    )


def fetch_search_page(
    *,
    raw_query: str,
    facets: str,
    offset: int = DEFAULT_OFFSET,
    limit: int = DEFAULT_LIMIT,
    sort: str | None = None,
    client: LegalHttpClient | None = None,
) -> SaijSearchPage:
    owns_client = client is None
    http = client or _make_client()
    params = search_params(
        raw_query=raw_query,
        facets=facets,
        offset=offset,
        limit=limit,
        sort=sort,
    )
    try:
        response = http.request("GET", SEARCH_URL, params=params)
        return parse_search_response(response)
    finally:
        if owns_client:
            http.close()


def fetch_document(
    *,
    guid: str,
    client: LegalHttpClient | None = None,
) -> SaijDocumentPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", VIEW_DOCUMENT_URL, params={"guid": guid})
        return parse_view_document_response(response, requested_guid=guid)
    finally:
        if owns_client:
            http.close()


def parse_search_response(response: httpx.Response) -> SaijSearchPage:
    payload = _json_payload(response, "SAIJ search response was not valid JSON")
    if not isinstance(payload, Mapping):
        raise parse_error(
            "SAIJ search payload must be a JSON object",
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    if payload.get("success") is False:
        raise parse_error(
            "SAIJ search returned an error payload",
            details={"url": str(response.url), "errors": payload.get("errors")},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"payload": dict(payload)}),
        )

    search_results = payload.get("searchResults")
    if not isinstance(search_results, Mapping):
        raise parse_error(
            "SAIJ search payload is missing searchResults",
            details={"url": str(response.url), "payload_keys": list(payload.keys())},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )

    raw_hits = search_results.get("documentResultList")
    if not isinstance(raw_hits, list):
        raise parse_error(
            "SAIJ search documentResultList must be a list",
            details={"url": str(response.url)},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    hits: list[JsonDict] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "SAIJ search hit must be a JSON object",
                details={"url": str(response.url), "hit_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[str(response.url)]),
            )
        hits.append(dict(hit))

    categories = search_results.get("categoriesResultList")
    facet_trees = parse_facets(categories)
    query_object = payload.get("queryObjectData")
    return SaijSearchPage(
        payload=dict(payload),
        hits=hits,
        total=total_from_facets(facet_trees),
        facets={
            "total": total_from_facets(facet_trees),
            "categories": facet_trees,
        },
        query_object=dict(query_object) if isinstance(query_object, Mapping) else {},
        fetched_url=str(response.url),
        headers=_useful_headers(response),
    )


def parse_view_document_response(response: httpx.Response, *, requested_guid: str | None = None) -> SaijDocumentPage:
    payload = _json_payload(response, "SAIJ view-document response was not valid JSON")
    fetched_url = str(response.url)
    provenance = _provenance(fetched_urls=[fetched_url], raw={"status_code": response.status_code})
    if not isinstance(payload, Mapping):
        raise parse_error(
            "SAIJ view-document payload must be a JSON object",
            provenance=provenance,
        )

    payload_dict = dict(payload)
    response_guid = _optional_text(payload_dict.get("guid")) or requested_guid
    raw_data = payload_dict.get("data")
    if raw_data is None or (isinstance(raw_data, str) and not raw_data.strip()):
        raise not_found(
            "SAIJ document was not found",
            details={"guid": response_guid or requested_guid},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=response_guid, raw={"payload": payload_dict}),
        )

    data_payload = _parse_view_data(raw_data, fetched_url=fetched_url, guid=response_guid or requested_guid)
    document = data_payload.get("document")
    if not isinstance(document, Mapping):
        if not data_payload:
            raise not_found(
                "SAIJ document was not found",
                details={"guid": response_guid or requested_guid},
                provenance=_provenance(
                    fetched_urls=[fetched_url],
                    source_response_id=response_guid,
                    raw={"payload": payload_dict},
                ),
            )
        raise parse_error(
            "SAIJ view-document data is missing document",
            details={"guid": response_guid or requested_guid, "data_keys": list(data_payload.keys())},
            provenance=_provenance(
                fetched_urls=[fetched_url],
                source_response_id=response_guid,
                raw={"payload": payload_dict},
            ),
        )

    metadata = document.get("metadata")
    content = document.get("content")
    if not isinstance(metadata, Mapping):
        raise parse_error(
            "SAIJ view-document document is missing metadata",
            details={"guid": response_guid or requested_guid, "document_keys": list(document.keys())},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=response_guid),
        )
    if not isinstance(content, Mapping):
        raise parse_error(
            "SAIJ view-document document is missing content",
            details={"guid": response_guid or requested_guid, "document_keys": list(document.keys())},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=response_guid),
        )

    metadata_dict = dict(metadata)
    document_guid = response_guid or _optional_text(metadata_dict.get("uuid"))
    if document_guid is None:
        raise parse_error(
            "SAIJ view-document response does not include a guid",
            details={"metadata_keys": list(metadata_dict.keys())},
            provenance=_provenance(fetched_urls=[fetched_url]),
        )

    return SaijDocumentPage(
        payload=payload_dict,
        data_payload=data_payload,
        document=dict(document),
        metadata=metadata_dict,
        content=dict(content),
        guid=document_guid,
        fetched_url=fetched_url,
        headers=_useful_headers(response),
    )


def search_params(
    *,
    raw_query: str,
    facets: str,
    offset: int = DEFAULT_OFFSET,
    limit: int = DEFAULT_LIMIT,
    sort: str | None = None,
) -> JsonDict:
    params: JsonDict = {
        "r": raw_query,
        "o": offset,
        "p": limit,
        "f": facets,
        "v": DEFAULT_VIEW,
    }
    if sort:
        params["s"] = sort
    return params


def parse_facets(value: Any) -> list[JsonDict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise parse_error("SAIJ categoriesResultList must be a list")
    return [_facet_node(item) for item in value if isinstance(item, Mapping)]


def total_from_facets(facets: list[JsonDict]) -> int | None:
    for facet in facets:
        if _search_key(facet.get("name")) != "total":
            continue
        for child in facet.get("children") or []:
            if isinstance(child, Mapping) and _search_key(child.get("name")) == "total":
                count = child.get("count")
                if isinstance(count, int) and not isinstance(count, bool):
                    return count
        count = facet.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return None


def hit_to_item(
    hit: Mapping[str, Any],
    *,
    search_page: SaijSearchPage | None = None,
    include_raw: bool = False,
) -> LegalItem:
    abstract = parse_document_abstract(hit)
    document = abstract.get("document")
    if not isinstance(document, Mapping):
        raise parse_error(
            "SAIJ documentAbstract is missing document",
            details={"uuid": _optional_text(hit.get("uuid"))},
            provenance=_provenance(
                fetched_urls=[search_page.fetched_url] if search_page is not None else [SEARCH_URL],
            ),
        )
    metadata = _mapping(document.get("metadata"))
    content = _mapping(document.get("content"))
    uuid = _document_uuid(hit, metadata)
    content_type = _optional_text(metadata.get("document-content-type"))
    friendly = _friendly_url(metadata.get("friendly-url"))
    display_url = f"{DOCUMENT_DISPLAY_URL}?guid={uuid}"
    title = _title(content, metadata, uuid)

    return LegalItem(
        id=f"{SOURCE_ID}:{uuid}",
        title=title,
        date=_content_date(content),
        document_type=content_type,
        url=display_url,
        snippet=_snippet(hit, content),
        facets={
            "content_type": content_type,
            "friendly_url": friendly.get("url"),
            "friendly_url_subdomain": friendly.get("subdomain"),
        },
        source_fields={
            "uuid": uuid,
            "guid": uuid,
            "content_type": content_type,
            "display_url": display_url,
            "friendly_url": friendly,
            "metadata": metadata,
            "content": content,
        },
        raw={"hit": dict(hit), "documentAbstract": abstract} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url] if search_page is not None else [SEARCH_URL],
            source_response_id=uuid,
            raw={
                "documentScore": hit.get("documentScore"),
                "explain": _optional_text(hit.get("explain")),
            },
        ),
    )


def document_page_to_document(document_page: SaijDocumentPage, *, include_raw: bool = False) -> LegalDocument:
    metadata = document_page.metadata
    content = document_page.content
    uuid = _optional_text(metadata.get("uuid")) or document_page.guid
    content_type = _optional_text(metadata.get("document-content-type"))
    friendly = _friendly_url(metadata.get("friendly-url"))
    display_url = _display_url(uuid)
    view_url = _view_url(uuid)
    body = _document_body(content)
    files = _attachment_files(content=content, metadata=metadata, guid=uuid)
    links = _document_links(
        display_url=display_url,
        view_url=view_url,
        friendly_url=friendly.get("url"),
        body=body,
        files=files,
    )
    source_fields: JsonDict = {
        "uuid": uuid,
        "guid": uuid,
        "content_type": content_type,
        "display_url": display_url,
        "view_url": view_url,
        "friendly_url": friendly,
        "metadata": metadata,
        "content": content,
    }

    return LegalDocument(
        id=f"{SOURCE_ID}:{uuid}",
        title=_title(content, metadata, uuid),
        date=_content_date(content),
        document_type=content_type,
        body=body,
        url=display_url,
        file_url=files[0]["url"] if files else None,
        content_type=content_type,
        text_format="plain_text" if body else None,
        metadata=_document_metadata(metadata=metadata, content=content, friendly=friendly, guid=uuid),
        links=links,
        files=files,
        source_fields=source_fields,
        raw={"payload": document_page.payload, "data": document_page.data_payload} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[document_page.fetched_url],
            source_response_id=uuid,
            raw={"headers": document_page.headers},
        ),
    )


def parse_document_abstract(hit: Mapping[str, Any]) -> JsonDict:
    raw = hit.get("documentAbstract")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise parse_error(
            "SAIJ search hit is missing documentAbstract",
            details={"uuid": _optional_text(hit.get("uuid"))},
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise parse_error(
            "SAIJ documentAbstract was not valid JSON",
            details={"uuid": _optional_text(hit.get("uuid")), "error": str(exc)},
        ) from exc
    if not isinstance(parsed, Mapping):
        raise parse_error(
            "SAIJ documentAbstract must decode to a JSON object",
            details={"uuid": _optional_text(hit.get("uuid")), "payload_type": type(parsed).__name__},
        )
    return dict(parsed)


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("SAIJ source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("facets", handle_facets, help="return SAIJ search facets", add_arguments=add_facets_arguments)
    adapter.register_operation("get", handle_get, help="fetch a SAIJ document by guid", add_arguments=add_get_arguments)
    adapter.register_operation("search", handle_search, help="search SAIJ", add_arguments=add_search_arguments)
    adapter.register_operation(
        "download",
        handle_download,
        help="download a SAIJ document PDF attachment when present",
        add_arguments=add_download_arguments,
    )
    return adapter


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": HUMAN_URL,
        }
    )


def _build_texto_query(text: str) -> str:
    """Build a relevance-friendly SAIJ ``texto`` raw query from free text.

    ``texto:<words>`` makes SAIJ AND only the first token into the ``texto``
    field and OR the rest into the default ``contenido`` field, so a multi-term
    query matches almost everything and ranks by recency. Instead we AND every
    term inside ``texto`` (``texto:(a AND b AND ...)``), which SAIJ expands to
    ``+texto:a +texto:b ...`` — every term required, results ranked by score.
    Quoted phrases in the input are preserved as phrase matches.
    """
    import shlex

    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    parts: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        parts.append(f'"{token}"' if " " in token else token)
    if not parts:
        return f"texto:{text}"
    if len(parts) == 1:
        return f"texto:{parts[0]}"
    return "texto:(" + " AND ".join(parts) + ")"


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    text = _optional_text(args.text)
    raw_query = _optional_text(args.raw_query)
    if raw_query is None:
        if text is None:
            raise usage_error("either --text or --raw-query is required")
        raw_query = _build_texto_query(text)

    document_type = _optional_text(args.document_type)
    facets = _facets_with_type(args.facets, document_type=document_type)
    query: JsonDict = {
        "text": text,
        "raw_query": raw_query,
        "facets": facets,
        "type": document_type,
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


def _facets_with_type(value: Any, *, document_type: str | None) -> str:
    parts = _split_facets(value) or _split_facets(DEFAULT_FACETS)
    type_facet = _type_facet(document_type)
    if type_facet is None:
        return "|".join(parts)

    root = _facet_root_key(type_facet)
    replaced = False
    output: list[str] = []
    for part in parts:
        if _facet_root_key(part) == root:
            if not replaced:
                output.append(type_facet)
                replaced = True
            continue
        output.append(part)
    if not replaced:
        output.append(type_facet)
    return "|".join(output)


def _type_facet(value: str | None) -> str | None:
    if not value:
        return None
    return TYPE_FACETS.get(_search_key(value), value)


def _split_facets(value: Any) -> list[str]:
    text = _optional_text(value)
    if not text:
        return []
    return [part for part in (clean_text(item) for item in text.split("|")) if part]


def _facet_root_key(value: str) -> str:
    return _search_key(value.split("/", 1)[0])


def _facet_node(value: Mapping[str, Any]) -> JsonDict:
    children = value.get("facetChildren")
    normalized: JsonDict = {
        "name": _optional_text(value.get("facetName")),
        "count": _int_or_none(value.get("facetHits")),
        "depth": _int_or_none(value.get("currentDepth")),
        "has_more": bool(value.get("hasMoreChildren")) if value.get("hasMoreChildren") is not None else False,
        "children": [_facet_node(child) for child in children if isinstance(child, Mapping)]
        if isinstance(children, list)
        else [],
    }
    return {key: item for key, item in normalized.items() if item not in (None, [])}


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"status_code": response.status_code}),
        ) from exc


def _parse_view_data(value: Any, *, fetched_url: str, guid: str | None) -> JsonDict:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise parse_error(
            "SAIJ view-document data must be a JSON string",
            details={"url": fetched_url, "guid": guid, "data_type": type(value).__name__},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=guid),
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise parse_error(
            "SAIJ view-document data was not valid JSON",
            details={"url": fetched_url, "guid": guid, "error": str(exc)},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=guid),
        ) from exc
    if not isinstance(parsed, Mapping):
        raise parse_error(
            "SAIJ view-document data must decode to a JSON object",
            details={"url": fetched_url, "guid": guid, "payload_type": type(parsed).__name__},
            provenance=_provenance(fetched_urls=[fetched_url], source_response_id=guid),
        )
    return dict(parsed)


def _document_uuid(hit: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    uuid = _optional_text(metadata.get("uuid")) or _optional_text(hit.get("uuid"))
    if not uuid:
        raise parse_error(
            "SAIJ search hit does not include a UUID",
            details={"hit_keys": list(hit.keys()), "metadata_keys": list(metadata.keys())},
        )
    return uuid


def _display_url(guid: str) -> str:
    return f"{DOCUMENT_DISPLAY_URL}?guid={guid}"


def _view_url(guid: str) -> str:
    return f"{VIEW_DOCUMENT_URL}?guid={guid}"


def _title(content: Mapping[str, Any], metadata: Mapping[str, Any], uuid: str) -> str:
    for value in (
        content.get("titulo"),
        content.get("title"),
        metadata.get("title"),
        metadata.get("titulo"),
    ):
        title = _optional_text(value)
        if title:
            return title
    return uuid


def _content_date(content: Mapping[str, Any]) -> str | None:
    for key in ("fecha", "fecha-publicacion", "fecha_publicacion", "fecha-dictamen"):
        normalized = normalize_date(_optional_text(content.get(key)))
        if normalized:
            return normalized
    return None


def _document_body(content: Mapping[str, Any]) -> str | None:
    for key in (
        "texto",
        "texto-completo",
        "texto_completo",
        "texto-original",
        "texto_original",
        "contenido",
        "sumario",
        "resumen",
        "abstract",
        "descripcion",
    ):
        body = _content_text(content.get(key))
        if body:
            return body

    for key, value in content.items():
        if "texto" in _search_key(key):
            body = _content_text(value)
            if body:
                return body
    return None


def _content_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    without_saij_markup = _SAIJ_MARKUP_RE.sub(" ", value)
    return clean_snippet(without_saij_markup)


def _document_metadata(
    *,
    metadata: Mapping[str, Any],
    content: Mapping[str, Any],
    friendly: Mapping[str, Any],
    guid: str,
) -> JsonDict:
    jurisdiction = content.get("jurisdiccion")
    normalized: JsonDict = {
        "uuid": guid,
        "guid": guid,
        "content_type": _optional_text(metadata.get("document-content-type")),
        "friendly_url": dict(friendly) if friendly else None,
        "id_infojus": _optional_text(content.get("id-infojus")),
        "number": _first_text(
            content.get("numero-sumario"),
            content.get("numero-norma"),
            content.get("numero"),
            content.get("numero-interno"),
            content.get("mecanografico"),
        ),
        "matter": _optional_text(content.get("materia")),
        "jurisdiction": _named_value(jurisdiction),
        "province": _optional_text(content.get("provincia")),
        "court": _first_text(content.get("tribunal"), content.get("tipo-tribunal"), content.get("instancia")),
        "publication": content.get("publicacion-codificada") if isinstance(content.get("publicacion-codificada"), Mapping) else None,
        "source": _optional_text(content.get("fuente")),
        "state": _optional_text(content.get("estado")),
    }
    return {key: value for key, value in normalized.items() if value not in (None, {}, [])}


def _document_links(
    *,
    display_url: str,
    view_url: str,
    friendly_url: str | None,
    body: str | None,
    files: list[JsonDict],
) -> list[JsonDict]:
    links: list[JsonDict] = [
        {"url": display_url, "label": "SAIJ display page", "kind": "page"},
        {"url": view_url, "label": "SAIJ view-document JSON", "kind": "data"},
    ]
    if friendly_url:
        links.append({"url": friendly_url, "label": "SAIJ friendly URL", "kind": "page"})
    links.extend({"url": item["url"], "label": item.get("label") or item.get("field") or item["url"], "kind": item["kind"]} for item in files)

    for extracted in extract_links(body, base_url=PUBLIC_BASE_URL):
        if extracted.get("url"):
            links.append(dict(extracted))

    deduped: list[JsonDict] = []
    seen: set[str] = set()
    for link in links:
        url = _optional_text(link.get("url"))
        if url is None or url in seen:
            continue
        seen.add(url)
        deduped.append({key: value for key, value in link.items() if value not in (None, "")})
    return deduped


def _attachment_files(*, content: Mapping[str, Any], metadata: Mapping[str, Any], guid: str) -> list[JsonDict]:
    files: list[JsonDict] = []
    for scope, value in (("content", content), ("metadata", metadata)):
        _collect_attachment_files(value, path=scope, guid=guid, files=files)

    deduped: list[JsonDict] = []
    seen: set[str] = set()
    for item in files:
        url = item.get("url")
        if not isinstance(url, str) or url in seen:
            continue
        seen.add(url)
        deduped.append(item)
    return deduped


def _collect_attachment_files(value: Any, *, path: str, guid: str, files: list[JsonDict]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if _is_attachment_key(key):
                _add_attachment_file(item, field=child_path, key=str(key), guid=guid, files=files)
            if isinstance(item, Mapping | list):
                _collect_attachment_files(item, path=child_path, guid=guid, files=files)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_attachment_files(item, path=f"{path}[{index}]", guid=guid, files=files)


def _add_attachment_file(value: Any, *, field: str, key: str, guid: str, files: list[JsonDict]) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _add_attachment_file(item, field=f"{field}[{index}]", key=key, guid=guid, files=files)
        return

    label = _optional_text(key)
    raw: JsonDict = {}
    if isinstance(value, Mapping):
        raw = dict(value)
        label = _first_text(value.get("label"), value.get("nombre"), value.get("name"), value.get("filename"), key)
        url = _first_url_value(value)
    else:
        url = value if isinstance(value, str) else None

    resolved = _source_url(url)
    if resolved is None:
        return

    kind = "pdf" if "pdf" in _search_key(key) else classify_link(resolved, base_url=PUBLIC_BASE_URL)
    item: JsonDict = {
        "url": resolved,
        "label": label,
        "kind": kind,
        "field": field,
    }
    if raw:
        item["raw"] = raw
    if guid:
        item["guid"] = guid
    files.append({item_key: item_value for item_key, item_value in item.items() if item_value not in (None, "", {})})


def _first_url_value(value: Mapping[str, Any]) -> str | None:
    for key, item in value.items():
        if isinstance(item, Mapping):
            nested = _first_url_value(item)
            if nested:
                return nested
        elif _URL_KEY_RE.search(str(key)):
            text = item if isinstance(item, str) else None
            if text:
                return text
    return None


def _source_url(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("descarga-archivo"):
        value = f"/{value}"
    return absolute_url(PUBLIC_BASE_URL, value)


def _is_attachment_key(value: Any) -> bool:
    key = _search_key(value).replace(" ", "_")
    return key in ATTACHMENT_KEYS or "adjunto" in key or "anexo" in key or key.endswith("_pdf")


def _snippet(hit: Mapping[str, Any], content: Mapping[str, Any]) -> str | None:
    explain = clean_snippet(_optional_text(hit.get("explain")), max_length=SNIPPET_LENGTH)
    if explain:
        return explain
    for key in ("texto", "resumen", "sumario", "abstract", "descripcion"):
        snippet = clean_snippet(_optional_text(content.get(key)), max_length=SNIPPET_LENGTH)
        if snippet:
            return snippet
    return None


def _friendly_url(value: Any) -> JsonDict:
    friendly = _mapping(value)
    subdomain = _optional_text(friendly.get("subdomain"))
    description = _optional_text(friendly.get("description"))
    normalized: JsonDict = {
        "subdomain": subdomain,
        "description": description,
    }
    if description:
        normalized["url"] = f"{PUBLIC_BASE_URL}/{description.lstrip('/')}"
    return {key: item for key, item in normalized.items() if item}


def _named_value(value: Any) -> JsonDict | None:
    if not isinstance(value, Mapping):
        text = _optional_text(value)
        return {"description": text} if text else None
    normalized: JsonDict = {
        "code": _optional_text(value.get("codigo") or value.get("code")),
        "description": _optional_text(value.get("descripcion") or value.get("texto") or value.get("name")),
    }
    return {key: item for key, item in normalized.items() if item} or dict(value)


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _optional_text(value)
        if text:
            return text
    return None


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _search_result_value(payload: Mapping[str, Any], key: str) -> Any:
    search_results = payload.get("searchResults")
    if isinstance(search_results, Mapping):
        return search_results.get(key)
    return None


def _has_more(*, total: int | None, offset: int, limit: int, item_count: int) -> bool:
    if total is None:
        return item_count >= limit
    return offset + item_count < total


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _search_key(value: Any) -> str:
    text = clean_text(str(value)) if value is not None else None
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


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
        source_urls=[HUMAN_URL, SEARCH_URL, DOCUMENT_DISPLAY_URL, VIEW_DOCUMENT_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


register_adapter(build_adapter(), replace=True)
