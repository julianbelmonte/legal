"""BCRA search-index adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from apps.legal.errors import LegalCliError, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import LegalDocument, LegalItem, LegalResponse, Provenance
from apps.legal.pagination import build_page_info, decode_cursor
from apps.legal.parsing import classify_link, clean_snippet, clean_text, normalize_date
from apps.legal.registry import get_source
from apps.legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "bcra"
SOURCE_MAP = "apps/legal/docs/bcra_normativa.md"
HUMAN_URL = "https://www.bcra.gob.ar/buscador/"
PUBLIC_BASE_URL = "https://www.bcra.gob.ar"
API_BASE_URL = "https://svc-index.bcra.gob.ar"
CATEGORIES_URL = f"{API_BASE_URL}/categories"
SEARCH_URL = f"{API_BASE_URL}/search"

DEFAULT_LIMIT = 10
DEFAULT_PAGE = 1
SNIPPET_LENGTH = 320
ALLOWED_DIRECT_HOST_SUFFIX = "bcra.gob.ar"

DISPLAY_CATEGORIES = (
    "Páginas",
    "Noticias",
    "Catálogo de datos",
    "Eventos",
    "Comunicaciones",
    "Textos ordenados",
    "Informes",
    "Estadísticas e indicadores",
    "Documentos",
)

JsonDict = dict[str, Any]

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_ID_RE = re.compile(r"[^a-z0-9_.:-]+")
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/(?P<total>\d+)\s*$")


@dataclass(frozen=True)
class BcraSearchPage:
    payload: JsonDict
    hits: list[JsonDict]
    total: int | None
    fetched_url: str
    headers: JsonDict
    category_filtered_client_side: bool = False


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text query passed as BCRA q")
    parser.add_argument("--category", help="BCRA category filter")
    parser.add_argument("--from-date", dest="from_date", help="ISO start date, YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="ISO end date, YYYY-MM-DD")
    parser.add_argument("--page", type=_positive_int, help="1-based BCRA result page")
    parser.add_argument("--size", type=_positive_int, help="alias for --limit")


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("direct_url", nargs="?", help="direct BCRA URL to describe")
    parser.add_argument("--url", dest="url", help="direct BCRA URL to describe")


def handle_filters(args: argparse.Namespace) -> LegalResponse:
    with _make_client() as client:
        categories, fetched_url, headers = fetch_categories(client=client)
    return LegalResponse(
        ok=True,
        source=SOURCE_ID,
        operation="filters",
        request={},
        facets={"categories": _category_facets(categories), "category": _category_facets(categories)},
        provenance=_provenance(
            fetched_urls=[fetched_url],
            raw={"headers": headers, "category_count": len(categories)},
        ),
        warnings=[],
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(args.cursor, operation="search")
    query = _query_from_args(args, cursor_payload=cursor_payload)
    limit = int(args.size or args.limit or cursor_payload.get("limit") or DEFAULT_LIMIT)
    page = int(args.page or cursor_payload.get("page") or DEFAULT_PAGE)

    with _make_client() as client:
        search_page = fetch_search_page(
            text=query.get("text"),
            category=query.get("category"),
            from_date=query.get("from_date"),
            to_date=query.get("to_date"),
            page=page,
            limit=limit,
            client=client,
        )

    items = [
        hit_to_item(hit, search_page=search_page, include_raw=bool(args.raw))
        for hit in search_page.hits
    ]
    has_more = _has_more(total=search_page.total, page=page, limit=limit, item_count=len(items))
    warnings = []
    if search_page.category_filtered_client_side:
        warnings.append("category request failed; results were filtered client-side from the fetched page")

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query={**query, "page": page, "limit": limit},
        items=items,
        page=build_page_info(
            source=SOURCE_ID,
            operation="search",
            page=page,
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
                "took": search_page.payload.get("took"),
                "timed_out": search_page.payload.get("timed_out"),
                "category_filtered_client_side": search_page.category_filtered_client_side,
            },
        ),
        facets=facets_from_search_payload(search_page.payload),
        warnings=warnings,
    )


def handle_download(args: argparse.Namespace) -> LegalResponse:
    direct_url = args.url or args.direct_url
    if not direct_url:
        raise usage_error("download requires --url or a direct_url argument")
    url = normalize_direct_url(direct_url)

    with _make_client() as client:
        response = fetch_direct_url_metadata(url, client=client)
    document = response_to_download_document(url, response=response, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request={"url": url},
        document=document,
        provenance=document.provenance,
    )


def fetch_categories(*, client: LegalHttpClient | None = None) -> tuple[list[str], str, JsonDict]:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", CATEGORIES_URL)
        payload = _json_payload(response, "BCRA categories response was not valid JSON")
        categories = parse_categories_payload(payload)
        return categories, str(response.url), _useful_headers(response)
    finally:
        if owns_client:
            http.close()


def parse_categories_payload(payload: Any) -> list[str]:
    if isinstance(payload, list):
        categories = [_optional_text(item) for item in payload]
        return [item for item in categories if item]
    if isinstance(payload, Mapping):
        for key in ("categories", "data", "items"):
            categories = payload.get(key)
            if isinstance(categories, list):
                return parse_categories_payload(categories)
        buckets = _category_buckets(payload)
        if buckets:
            return [str(item["value"]) for item in buckets if item.get("value")]
    raise parse_error(
        "BCRA categories payload had an unexpected shape",
        details={"payload_type": type(payload).__name__},
        provenance=_provenance(fetched_urls=[CATEGORIES_URL]),
    )


def fetch_search_page(
    *,
    text: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = DEFAULT_PAGE,
    limit: int = DEFAULT_LIMIT,
    client: LegalHttpClient | None = None,
) -> BcraSearchPage:
    owns_client = client is None
    http = client or _make_client()
    canonical_category = canonical_category_value(category)
    params = search_params(
        text=text,
        category=canonical_category,
        from_date=from_date,
        to_date=to_date,
        page=page,
        limit=limit,
    )
    try:
        try:
            response = http.request("GET", SEARCH_URL, params=params)
            return parse_search_response(response)
        except LegalCliError as exc:
            if canonical_category is None or _status_code(exc) != 400:
                raise
            retry_params = dict(params)
            retry_params.pop("category", None)
            response = http.request("GET", SEARCH_URL, params=retry_params)
            search_page = parse_search_response(response)
            filtered = [
                hit
                for hit in search_page.hits
                if canonical_category_value(_source(hit).get("category")) == canonical_category
            ]
            return BcraSearchPage(
                payload=search_page.payload,
                hits=filtered,
                total=len(filtered),
                fetched_url=search_page.fetched_url,
                headers=search_page.headers,
                category_filtered_client_side=True,
            )
    finally:
        if owns_client:
            http.close()


def parse_search_response(response: httpx.Response) -> BcraSearchPage:
    payload = _json_payload(response, "BCRA search response was not valid JSON")
    if not isinstance(payload, Mapping):
        raise parse_error(
            "BCRA search payload must be a JSON object",
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    hits_obj = payload.get("hits")
    if not isinstance(hits_obj, Mapping):
        raise parse_error(
            "BCRA search payload is missing hits",
            details={"url": str(response.url)},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"payload_keys": list(payload.keys())}),
        )
    raw_hits = hits_obj.get("hits")
    if not isinstance(raw_hits, list):
        raise parse_error(
            "BCRA search hits must be a list",
            details={"url": str(response.url)},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    hits: list[JsonDict] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "BCRA search hit must be a JSON object",
                details={"url": str(response.url), "hit_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[str(response.url)]),
            )
        hits.append(dict(hit))
    return BcraSearchPage(
        payload=dict(payload),
        hits=hits,
        total=_total_from_hits(hits_obj.get("total")),
        fetched_url=str(response.url),
        headers=_useful_headers(response),
    )


def search_params(
    *,
    text: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = DEFAULT_PAGE,
    limit: int = DEFAULT_LIMIT,
) -> JsonDict:
    params: JsonDict = {"page": page, "size": limit}
    if text:
        params["q"] = text
    if category:
        params["category"] = category
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    return params


def hit_to_item(
    hit: Mapping[str, Any],
    *,
    search_page: BcraSearchPage | None = None,
    include_raw: bool = False,
) -> LegalItem:
    source = _source(hit)
    url = _source_url(source)
    content_type = _content_type(source)
    return LegalItem(
        id=_hit_id(hit, source),
        title=_title(source, url),
        date=_source_date(source),
        document_type=_optional_text(source.get("category")) or _kind_from_url(url, content_type),
        url=url,
        file_url=url if _is_file_url(url, content_type) else None,
        snippet=_snippet(hit, source),
        facets={
            "category": _optional_text(source.get("category")),
            "content_type": content_type,
            "extension": _file_field(source, "extension"),
        },
        source_fields=dict(source),
        raw=dict(hit) if include_raw else {},
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url] if search_page is not None else [SEARCH_URL],
            source_response_id=_optional_text(hit.get("_id")),
            raw={
                "index": hit.get("_index"),
                "score": hit.get("_score"),
            },
        ),
    )


def facets_from_search_payload(payload: Mapping[str, Any]) -> JsonDict:
    facets: JsonDict = {}
    buckets = _category_buckets(payload)
    if buckets:
        facets["categories"] = buckets
        facets["category"] = buckets
    aggregations = payload.get("aggregations")
    if isinstance(aggregations, Mapping):
        all_bucket = aggregations.get("all")
        if isinstance(all_bucket, Mapping) and isinstance(all_bucket.get("doc_count"), int):
            facets["total_all_categories"] = all_bucket["doc_count"]
    return facets


def fetch_direct_url_metadata(url: str, *, client: LegalHttpClient | None = None) -> httpx.Response:
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


def response_to_download_document(
    url: str,
    *,
    response: httpx.Response,
    include_raw: bool = False,
) -> LegalDocument:
    content_type = _optional_text(response.headers.get("content-type"))
    filename = _filename_from_url(url)
    content_length = _content_length(response.headers)
    metadata: JsonDict = {
        "url": url,
        "filename": filename,
        "extension": _extension(filename),
        "kind": classify_link(url, base_url=PUBLIC_BASE_URL, content_type=content_type),
        "content_type": content_type,
        "content_length": content_length,
        "last_modified": _optional_text(response.headers.get("last-modified")),
        "etag": _optional_text(response.headers.get("etag")),
        "method": response.request.method,
        "status_code": response.status_code,
    }
    return LegalDocument(
        id=_download_id(url),
        title=filename or url,
        document_type=metadata["kind"],
        url=url,
        file_url=url if metadata["kind"] != "page" else None,
        content_type=content_type,
        metadata={key: value for key, value in metadata.items() if value is not None},
        links=[{"url": url, "label": filename or url, "kind": metadata["kind"]}],
        source_fields={"direct_url": url},
        raw={"headers": dict(response.headers)} if include_raw else {},
        provenance=_provenance(
            fetched_urls=[str(response.url)],
            source_response_id=_download_id(url),
            raw={
                "method": response.request.method,
                "status_code": response.status_code,
            },
        ),
    )


def normalize_direct_url(value: str) -> str:
    raw = clean_text(value)
    if not raw:
        raise usage_error("download URL must not be empty")
    if raw.startswith("/"):
        raw = f"{PUBLIC_BASE_URL}{raw}"
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise usage_error("download URL must use http or https", details={"url": value})
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host != ALLOWED_DIRECT_HOST_SUFFIX and not host.endswith(f".{ALLOWED_DIRECT_HOST_SUFFIX}"):
        raise usage_error("download URL must point to bcra.gob.ar", details={"url": value})
    return raw


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError("BCRA source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("filters", handle_filters, help="list BCRA filter values")
    adapter.register_operation("search", handle_search, help="search the BCRA index", add_arguments=add_search_arguments)
    adapter.register_operation(
        "download",
        handle_download,
        help="return metadata for a direct BCRA URL",
        add_arguments=add_download_arguments,
    )
    return adapter


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Accept": "application/json,text/plain,*/*"})


def _query_from_args(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw") if cursor_payload else None
    if isinstance(raw, Mapping) and isinstance(raw.get("query"), Mapping):
        return {str(key): value for key, value in raw["query"].items() if value not in (None, "")}

    from_date = _iso_date(args.from_date, field="from_date")
    to_date = _iso_date(args.to_date, field="to_date")
    if from_date and to_date and from_date > to_date:
        raise usage_error("--from-date must be earlier than or equal to --to-date")

    query = {
        "text": _optional_text(args.text),
        "category": canonical_category_value(args.category),
        "from_date": from_date,
        "to_date": to_date,
    }
    return {key: value for key, value in query.items() if value not in (None, "")}


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _iso_date(value: str | None, *, field: str) -> str | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise usage_error(f"--{field.replace('_', '-')} must be an ISO date YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return parsed


def canonical_category_value(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    lookup = {_search_key(category): category for category in DISPLAY_CATEGORIES}
    return lookup.get(_search_key(text), text)


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"status_code": response.status_code}),
        ) from exc


def _source(hit: Mapping[str, Any]) -> JsonDict:
    source = hit.get("_source")
    if isinstance(source, Mapping):
        return dict(source)
    return {}


def _source_url(source: Mapping[str, Any]) -> str | None:
    url = _optional_text(source.get("url"))
    if url:
        return url
    path = source.get("path")
    if isinstance(path, Mapping):
        virtual = _optional_text(path.get("virtual"))
        if virtual:
            return f"{PUBLIC_BASE_URL}/archivos{virtual}" if virtual.startswith("/") else f"{PUBLIC_BASE_URL}/{virtual}"
    return None


def _hit_id(hit: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    hit_id = _optional_text(hit.get("_id"))
    if hit_id:
        return f"{SOURCE_ID}:{hit_id}"
    url = _source_url(source)
    if url:
        return f"{SOURCE_ID}:{_slug(urlparse(url).path)}"
    return f"{SOURCE_ID}:unknown"


def _title(source: Mapping[str, Any], url: str | None) -> str | None:
    for key in ("title", "titulo", "name", "nombre", "description", "descripcion"):
        text = _optional_text(source.get(key))
        if text:
            return text
    filename = _file_field(source, "filename") or _filename_from_url(url)
    category = _optional_text(source.get("category"))
    if category and filename:
        return f"{category} {filename}"
    return filename or url


def _source_date(source: Mapping[str, Any]) -> str | None:
    for value in (
        source.get("date"),
        source.get("published_at"),
        source.get("publication_date"),
        _mapping_value(source.get("meta"), "created"),
        _file_field(source, "last_modified"),
        _file_field(source, "created"),
        _file_field(source, "indexing_date"),
    ):
        normalized = normalize_date(str(value)) if value is not None else None
        if normalized:
            return normalized
    return None


def _snippet(hit: Mapping[str, Any], source: Mapping[str, Any]) -> str | None:
    highlight = hit.get("highlight")
    if isinstance(highlight, Mapping):
        snippets: list[str] = []
        for value in highlight.values():
            if isinstance(value, list):
                snippets.extend(str(item) for item in value)
            elif value is not None:
                snippets.append(str(value))
        snippet = clean_snippet(" ... ".join(snippets), max_length=SNIPPET_LENGTH)
        if snippet:
            return snippet
    for key in ("snippet", "summary", "description", "content"):
        snippet = clean_snippet(_optional_text(source.get(key)), max_length=SNIPPET_LENGTH)
        if snippet:
            return snippet
    return None


def _content_type(source: Mapping[str, Any]) -> str | None:
    return _file_field(source, "content_type") or _optional_text(source.get("content_type"))


def _file_field(source: Mapping[str, Any], key: str) -> str | None:
    file_obj = source.get("file")
    if isinstance(file_obj, Mapping):
        return _optional_text(file_obj.get(key))
    return None


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return None


def _is_file_url(url: str | None, content_type: str | None) -> bool:
    return _kind_from_url(url, content_type) not in {"page", "unknown"}


def _kind_from_url(url: str | None, content_type: str | None) -> str | None:
    kind = classify_link(url, base_url=PUBLIC_BASE_URL, content_type=content_type)
    return None if kind == "unknown" else kind


def _category_buckets(payload: Mapping[str, Any]) -> list[JsonDict]:
    aggregations = payload.get("aggregations")
    if not isinstance(aggregations, Mapping):
        return []
    categories = aggregations.get("categories")
    if not isinstance(categories, Mapping):
        return []
    buckets = categories.get("buckets")
    if isinstance(buckets, Mapping):
        return [
            {"value": str(key), "count": _doc_count(value)}
            for key, value in buckets.items()
        ]
    if isinstance(buckets, list):
        normalized = []
        for bucket in buckets:
            if not isinstance(bucket, Mapping):
                continue
            key = bucket.get("key") or bucket.get("value")
            if key is None:
                continue
            normalized.append({"value": str(key), "count": _doc_count(bucket)})
        return normalized
    return []


def _doc_count(value: Any) -> int | None:
    if isinstance(value, Mapping):
        raw = value["doc_count"] if "doc_count" in value else value.get("count")
    else:
        raw = value
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _total_from_hits(total: Any) -> int | None:
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    if isinstance(total, Mapping):
        value = total.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _has_more(*, total: int | None, page: int, limit: int, item_count: int) -> bool:
    if total is None:
        return item_count >= limit
    return page * limit < total


def _category_facets(categories: Sequence[str]) -> list[JsonDict]:
    return [{"value": category, "label": category} for category in categories]


def _content_length(headers: httpx.Headers) -> int | None:
    raw_length = headers.get("content-length")
    if raw_length and raw_length.isdigit():
        return int(raw_length)
    content_range = headers.get("content-range")
    if content_range:
        match = _CONTENT_RANGE_TOTAL_RE.search(content_range)
        if match:
            return int(match.group("total"))
    return None


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return clean_text(name)


def _extension(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[-1].lower()


def _download_id(url: str) -> str:
    return f"{SOURCE_ID}:download:{_slug(urlparse(url).path)}"


def _slug(value: Any) -> str:
    slug = _ID_RE.sub("-", _search_key(value).replace(" ", "-")).strip("-")
    return slug[:120] or "url"


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


def _useful_headers(response: httpx.Response) -> JsonDict:
    useful = {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower()
        in {
            "content-type",
            "etag",
            "last-modified",
            "ratelimit-limit",
            "ratelimit-policy",
            "ratelimit-remaining",
            "ratelimit-reset",
        }
    }
    return dict(useful)


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
        source_urls=[HUMAN_URL, API_BASE_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


register_adapter(build_adapter(), replace=True)
