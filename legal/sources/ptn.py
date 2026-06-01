"""PTN dictamenes direct search adapter."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from legal import enrichment
from legal.captcha import CaptchaError, solve_recaptcha_v3
from legal.errors import CAPTCHA_SOLVER_CAPABILITY, LegalCliError, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import decode_cursor, make_cursor
from legal.parsing import clean_snippet, clean_text, normalize_date
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "ptn"
SOURCE_MAP = "legal/docs/ptn_dictamenes.md"

API_BASE_URL = "https://api.ptn.gob.ar"
HUMAN_URL = "https://busquedadictamenes.ptn.gob.ar/"
SEARCH_URL = f"{API_BASE_URL}/search"
CONFIRM_TOKEN_URL = f"{API_BASE_URL}/confirmToken"
RECAPTCHA_SITEKEY = "6LckcgYaAAAAABSSzWzlfmJcP2YbfC6scSodMGC6"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 420
TEXT_HIGHLIGHT_FIELDS = (
    "voces",
    "attachments.attachment.content",
    "organismo",
    "array_leyes",
    "array_decretos",
    "numero",
    "expediente",
    "tomo",
    "pagina",
    "fecha",
)
_LAW_RE = re.compile(r"\bley\s*n?[°º.]?\s*([0-9]+\.[0-9]+|[0-9]+)", re.IGNORECASE)
_DECREE_RE = re.compile(r"\bdecreto\s*n?[°º.]?\s*([0-9]+\.[0-9]+|[0-9]+[\/]?[0-9]*)", re.IGNORECASE)


@dataclass(frozen=True)
class PtnSearchPage:
    payload: JsonDict
    hits: list[JsonDict]
    total: int | None
    total_relation: str | None
    aggregations: JsonDict
    fetched_url: str
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text query")
    parser.add_argument(
        "--historico",
        nargs="?",
        const=True,
        default=None,
        type=_bool_arg,
        help="include the historical index; default true",
    )
    parser.add_argument(
        "--solo-historico",
        nargs="?",
        const=True,
        default=None,
        type=_bool_arg,
        help="query only the historical index; default false",
    )
    parser.add_argument("--tomo", help="tomo filter")
    parser.add_argument("--pagina", help="pagina filter")
    parser.add_argument("--numero", help="dictamen number filter")
    parser.add_argument("--expediente", help="expediente filter")
    parser.add_argument("--raw-body", help="JSON object merged into the generated Elasticsearch body")


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="id", help="PTN search hit _id")
    parser.add_argument(
        "--type",
        dest="file_type",
        default="dictamen",
        help="attachment file_type, usually dictamen or doctrina; default dictamen",
    )
    parser.add_argument(
        "--historical",
        "--historico",
        dest="historical",
        nargs="?",
        const=True,
        default=None,
        type=_bool_arg,
        help="whether the protected file lives in the historical index; default false",
    )
    enrichment.add_text_arguments(parser)


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None))
    explicit_query = _has_explicit_query_args(args)
    cursor_state = _cursor_state(cursor_payload) if cursor_payload and not explicit_query else None
    raw_body = _parse_raw_body(getattr(args, "raw_body", None))

    if cursor_state is not None:
        query = cursor_state["query"]
        historico = _bool_or_default(cursor_state.get("historico"), default=True)
        solo_historico = _bool_or_default(cursor_state.get("solo_historico"), default=False)
        offset = _cursor_offset(cursor_payload)
        limit = _resolve_limit(args, cursor_payload=cursor_payload, body=cursor_state["body"])
        body = _json_copy(cursor_state["body"])
    else:
        query = build_query(
            text=getattr(args, "text", None),
            tomo=getattr(args, "tomo", None),
            pagina=getattr(args, "pagina", None),
            numero=getattr(args, "numero", None),
            expediente=getattr(args, "expediente", None),
            raw_body=raw_body,
        )
        historico = _bool_or_default(getattr(args, "historico", None), default=True)
        solo_historico = _bool_or_default(getattr(args, "solo_historico", None), default=False)
        offset = 0
        limit = _resolve_limit(args, cursor_payload={}, body=raw_body)
        body = build_search_body(query, offset=offset, limit=limit)

    if raw_body:
        body = _deep_merge(body, raw_body)
    body["from"] = offset
    body["size"] = limit

    with _make_client() as client:
        search_page = fetch_search_page(
            body,
            historico=historico,
            solo_historico=solo_historico,
            client=client,
        )

    items = [hit_to_item(hit, search_page=search_page, include_raw=bool(args.raw)) for hit in search_page.hits]
    next_cursor = _next_cursor(
        body=body,
        query=query,
        historico=historico,
        solo_historico=solo_historico,
        total=search_page.total,
        offset=offset,
        limit=limit,
        item_count=len(items),
    )
    response_query = _response_query(
        query,
        historico=historico,
        solo_historico=solo_historico,
        limit=limit,
        offset=offset,
    )
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query=response_query,
        items=items,
        page=PageInfo(
            limit=limit,
            offset=offset,
            total=search_page.total,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=_source_response_id(query, offset),
            raw={
                "headers": search_page.headers,
                "request_body": body,
                "total_relation": search_page.total_relation,
                "took": search_page.payload.get("took"),
                "timed_out": search_page.payload.get("timed_out"),
                "returned_hits": len(search_page.hits),
            },
        ),
        warnings=[],
        facets=normalize_aggregations(search_page.aggregations),
    )


def handle_download(args: argparse.Namespace) -> LegalResponse:
    document_id = _optional_text(getattr(args, "id", None))
    # search items expose their id as "ptn:<hit_id>"; accept it verbatim
    if document_id and document_id.startswith(f"{SOURCE_ID}:"):
        document_id = document_id.split(":", 1)[1]
    file_type = _optional_text(getattr(args, "file_type", None)) or "dictamen"
    historical = _bool_or_default(getattr(args, "historical", None), default=False)
    want_text = bool(getattr(args, "want_text", False))
    save_path = getattr(args, "save_pdf", None) or None

    if not document_id:
        raise usage_error(
            "download requires --id",
            details={
                "source": SOURCE_ID,
                "operation": "download",
                "type": file_type,
                "historical": historical,
            },
        )

    request = _compact(
        {
            "id": document_id,
            "type": file_type,
            "historical": historical,
            "text": True if want_text else None,
            "save_pdf": save_path,
        }
    )
    token = _solve_download_captcha(document_id=document_id, file_type=file_type, historical=historical)

    params = {
        "token": token,
        "id": document_id,
        "type": file_type,
        "historical": _bool_param(historical),
    }
    with _make_client() as client:
        response = client.request("POST", CONFIRM_TOKEN_URL, params=params)
        payload = parse_confirm_token_response(response)
        file_path = _required_file_path(payload)
        file_url = _file_url(file_path)
        pdf_response = client.request("GET", file_url, headers=_pdf_headers())
        pdf_bytes = pdf_response.content

    enrichment_fields = enrichment.finalize_document(
        pdf_bytes,
        want_text=want_text,
        save_path=save_path,
    )
    text_value = enrichment_fields.get("text")
    text = text_value if isinstance(text_value, str) and text_value.strip() else None

    document_key = f"{SOURCE_ID}:download:{document_id}:{file_type}"
    headers = _useful_headers(response)
    pdf_headers = _useful_headers(pdf_response)
    document = LegalDocument(
        id=document_key,
        title=f"PTN {file_type} {document_id}",
        document_type=file_type,
        body=text,
        url=HUMAN_URL,
        file_url=file_url,
        content_type="application/pdf" if file_url.lower().endswith(".pdf") else None,
        text_format="plain_text" if text else None,
        metadata=_compact(
            {
                "document_id": document_id,
                "type": file_type,
                "historical": historical,
                "file": file_path,
                "file_url": file_url,
            }
        )
        | enrichment_fields,
        links=[{"url": file_url, "label": file_type, "kind": "pdf"}],
        files=[
            _compact(
                {
                    "url": file_url,
                    "label": f"{file_type} PDF",
                    "kind": "pdf",
                    "content_type": "application/pdf" if file_url.lower().endswith(".pdf") else None,
                }
            )
        ],
        source_fields=_compact(
            {
                "document_id": document_id,
                "type": file_type,
                "historical": historical,
                "file": file_path,
                "confirm_endpoint": CONFIRM_TOKEN_URL,
            }
        ),
        raw={"confirm_response": payload, "headers": headers, "pdf_headers": pdf_headers} if bool(args.raw) else {},
        provenance=_provenance(
            fetched_urls=[CONFIRM_TOKEN_URL, file_url],
            source_response_id=document_key,
            raw={
                "headers": headers,
                "pdf_headers": pdf_headers,
                "confirm_response_keys": sorted(str(key) for key in payload.keys()),
                "token_redacted": True,
            },
        ),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request=request,
        document=document,
        provenance=document.provenance,
    )


def fetch_search_page(
    body: Mapping[str, Any],
    *,
    historico: bool,
    solo_historico: bool,
    client: LegalHttpClient | None = None,
) -> PtnSearchPage:
    owns_client = client is None
    http = client or _make_client()
    params = {
        "historico": _bool_param(historico),
        "solo_historico": _bool_param(solo_historico),
    }
    try:
        response = http.request("POST", SEARCH_URL, params=params, json=dict(body))
        return parse_search_response(response)
    finally:
        if owns_client:
            http.close()


def parse_confirm_token_response(response: httpx.Response) -> JsonDict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise parse_error(
            "PTN confirmToken response was not valid JSON",
            details={"url": CONFIRM_TOKEN_URL, "status_code": response.status_code},
            provenance=_provenance(
                fetched_urls=[CONFIRM_TOKEN_URL],
                raw={"status_code": response.status_code},
            ),
        ) from exc
    if not isinstance(payload, Mapping):
        raise parse_error(
            "PTN confirmToken payload must be a JSON object",
            details={"payload_type": type(payload).__name__},
            provenance=_provenance(fetched_urls=[CONFIRM_TOKEN_URL]),
        )
    return dict(payload)


def parse_search_response(response: httpx.Response) -> PtnSearchPage:
    payload = _json_payload(response, "PTN search response was not valid JSON")
    if not isinstance(payload, Mapping):
        raise parse_error(
            "PTN search payload must be a JSON object",
            details={"payload_type": type(payload).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    payload = dict(payload)
    if isinstance(payload.get("error"), Mapping):
        error = payload["error"]
        raise parse_error(
            "PTN search returned an Elasticsearch error",
            details={
                "status": payload.get("status"),
                "error_type": error.get("type"),
                "reason": error.get("reason"),
            },
            provenance=_provenance(
                fetched_urls=[str(response.url)],
                raw={"status": payload.get("status"), "error": _compact_error(error)},
            ),
        )

    hits_payload = payload.get("hits")
    if not isinstance(hits_payload, Mapping):
        raise parse_error(
            "PTN search payload is missing hits",
            details={"payload_keys": list(payload.keys())},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )
    raw_hits = hits_payload.get("hits")
    if not isinstance(raw_hits, list):
        raise parse_error(
            "PTN search hits must be a JSON array",
            details={"hits_type": type(raw_hits).__name__},
            provenance=_provenance(fetched_urls=[str(response.url)]),
        )

    hits: list[JsonDict] = []
    for hit in raw_hits:
        if not isinstance(hit, Mapping):
            raise parse_error(
                "PTN search hit entries must be JSON objects",
                details={"hit_type": type(hit).__name__},
                provenance=_provenance(fetched_urls=[str(response.url)]),
            )
        hits.append(dict(hit))

    total, relation = _total_hits(hits_payload.get("total"))
    aggregations = payload.get("aggregations")
    return PtnSearchPage(
        payload=payload,
        hits=hits,
        total=total,
        total_relation=relation,
        aggregations=dict(aggregations) if isinstance(aggregations, Mapping) else {},
        fetched_url=str(response.url),
        headers=_useful_headers(response),
    )


def build_query(
    *,
    text: str | None = None,
    tomo: str | None = None,
    pagina: str | None = None,
    numero: str | None = None,
    expediente: str | None = None,
    raw_body: Mapping[str, Any] | None = None,
) -> JsonDict:
    query = _compact(
        {
            "text": _optional_text(text),
            "tomo": _optional_text(tomo),
            "pagina": _optional_text(pagina),
            "numero": _optional_text(numero),
            "expediente": _optional_text(expediente),
        }
    )
    if raw_body:
        query["raw_body"] = True
        query["raw_body_keys"] = sorted(str(key) for key in raw_body)
    return query


def build_search_body(query: Mapping[str, Any], *, offset: int, limit: int) -> JsonDict:
    must: list[JsonDict] = []
    filters: list[JsonDict] = []
    text = _optional_text(query.get("text"))
    if text:
        must.extend(_text_clauses(text))
    filters.extend(_field_filters(query))
    if not must and not filters:
        must.append({"match_all": {}})

    bool_query: JsonDict = {"must": must}
    if filters:
        bool_query["filter"] = filters
    return {
        "highlight": {
            "fragment_size": 200,
            "number_of_fragments": 5,
            "fields": {field: {} for field in TEXT_HIGHLIGHT_FIELDS},
        },
        "aggs": _aggregations_body(),
        "query": {"bool": bool_query},
        "sort": [{"fecha": "desc"}],
        "from": offset,
        "size": limit,
    }


def hit_to_item(hit: Mapping[str, Any], *, search_page: PtnSearchPage, include_raw: bool = False) -> LegalItem:
    hit_id = _required_text(hit.get("_id"), field="_id")
    source = _mapping(hit.get("_source"))
    index = _optional_text(hit.get("_index"))
    numero = _optional_text(source.get("numero"))
    tomo = _optional_text(source.get("tomo"))
    pagina = _optional_text(source.get("pagina"))
    expediente = _optional_text(source.get("expediente"))
    organismo = _string_list(source.get("organismo"))
    voces = _string_list(source.get("voces"))
    attachments = attachment_metadata(source.get("attachments"))
    file_types = _unique([_optional_text(item.get("file_type")) for item in attachments])
    highlight = _mapping(hit.get("highlight"))

    return LegalItem(
        id=f"{SOURCE_ID}:{hit_id}",
        title=_title(numero=numero, tomo=tomo, pagina=pagina, expediente=expediente),
        date=normalize_date(_optional_text(source.get("fecha"))),
        document_type="dictamen",
        url=HUMAN_URL,
        snippet=_snippet(highlight, attachments=attachments),
        facets=_compact(
            {
                "index": index,
                "tomo": tomo,
                "pagina": pagina,
                "organismo": organismo,
                "voces": voces,
                "file_types": file_types,
            }
        ),
        source_fields=_compact(
            {
                "_id": hit_id,
                "_index": index,
                "_score": hit.get("_score"),
                "sort": hit.get("sort"),
                "numero": numero,
                "tomo": tomo,
                "pagina": pagina,
                "fecha": _optional_text(source.get("fecha")),
                "expediente": expediente,
                "organismo": organismo,
                "voces": voces,
                "doctrinas_asociadas": source.get("doctrinas_asociadas"),
                "leyes": source.get("leyes"),
                "array_leyes": _string_list(source.get("array_leyes")),
                "array_decretos": _string_list(source.get("array_decretos")),
                "highlight": highlight,
                "attachments": attachments,
                "download": _compact(
                    {
                        "operation": "download",
                        "file_types": file_types,
                        "historical": _historical_from_index(index),
                        "captcha": "recaptcha_v3_internal",
                    }
                ),
            }
        ),
        raw=dict(hit) if include_raw else {},
        provenance=_provenance(
            fetched_urls=[search_page.fetched_url],
            source_response_id=f"{index}:{hit_id}" if index else hit_id,
        ),
    )


def attachment_metadata(value: Any) -> list[JsonDict]:
    if not isinstance(value, list):
        return []
    entries: list[JsonDict] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        attachment = _mapping(item.get("attachment"))
        content = _optional_text(attachment.get("content"))
        entries.append(
            _compact(
                {
                    "index": index,
                    "file_type": _optional_text(item.get("file_type")),
                    "content_type": _optional_text(attachment.get("content_type")),
                    "language": _optional_text(attachment.get("language")),
                    "content": content,
                    "content_length": len(content) if content else None,
                    "content_snippet": clean_snippet(content, max_length=SNIPPET_LENGTH) if content else None,
                    "attachment_keys": sorted(str(key) for key in attachment.keys()) if attachment else None,
                }
            )
        )
    return entries


def normalize_aggregations(aggregations: Mapping[str, Any]) -> JsonDict:
    facets: JsonDict = {}
    for name, value in aggregations.items():
        if not isinstance(value, Mapping):
            continue
        buckets = value.get("buckets")
        if not isinstance(buckets, list):
            continue
        if name == "tomo":
            facets[name] = [_tomo_bucket(bucket) for bucket in buckets if isinstance(bucket, Mapping)]
        else:
            facets[name] = [_bucket(bucket) for bucket in buckets if isinstance(bucket, Mapping)]
    return {key: value for key, value in facets.items() if value}


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"source {SOURCE_ID!r} is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("search", handle_search, help="search PTN dictamenes API", add_arguments=add_search_arguments)
    adapter.register_operation(
        "download",
        handle_download,
        help="resolve a PTN file URL with internal reCAPTCHA v3 solving",
        add_arguments=add_download_arguments,
    )
    return adapter


def _text_clauses(text: str) -> list[JsonDict]:
    ley = _legal_reference(_LAW_RE, text)
    decreto = _legal_reference(_DECREE_RE, text)
    return [
        {
            "dis_max": {
                "queries": [
                    {
                        "match": {
                            "voces": {
                                "query": text,
                                "operator": "or",
                                "minimum_should_match": "75%",
                                "boost": 10,
                            }
                        }
                    },
                    {
                        "match": {
                            "organismo": {
                                "query": text,
                                "operator": "or",
                                "minimum_should_match": "75%",
                                "boost": 3,
                            }
                        }
                    },
                    {"match": {"attachments.attachment.content": {"query": text, "operator": "and"}}},
                    {"match": {"array_leyes": {"query": ley or " ", "boost": 100}}},
                    {"match": {"array_decretos": {"query": decreto or " ", "boost": 100}}},
                    {
                        "multi_match": {
                            "query": text,
                            "fields": ["tomo", "pagina", "fecha", "numero", "expediente"],
                            "operator": "and",
                            "lenient": True,
                            "boost": 2,
                        }
                    },
                ],
                "tie_breaker": 0.3,
            }
        }
    ]


def _field_filters(query: Mapping[str, Any]) -> list[JsonDict]:
    filters: list[JsonDict] = []
    tomo = _optional_text(query.get("tomo"))
    pagina = _optional_text(query.get("pagina"))
    numero = _optional_text(query.get("numero"))
    expediente = _optional_text(query.get("expediente"))
    if tomo:
        filters.append(_numeric_regexp_filter("tomo", tomo))
    if pagina:
        filters.append(_numeric_regexp_filter("pagina", pagina))
    if numero:
        filters.append({"term": {"numero": numero}})
    if expediente:
        filters.append({"match_phrase": {"expediente": expediente}})
    return filters


def _numeric_regexp_filter(field: str, value: str) -> JsonDict:
    normalized = value.replace(" ", "").strip()
    if not normalized:
        raise usage_error(f"--{field} must not be blank")
    stripped = normalized.lstrip("0") or "0"
    pattern = f"0{{0,{3 - len(stripped)}}}{stripped}" if len(stripped) < 3 else stripped
    return {"regexp": {field: {"value": pattern, "case_insensitive": True}}}


def _aggregations_body() -> JsonDict:
    year_script = "if (doc.containsKey('fecha')) { doc['fecha'].value.getYear() }"
    month_script = "if (doc.containsKey('fecha')) { doc['fecha'].value.getMonth() }"
    return {
        "organismo": {"terms": {"field": "organismo.keyword", "size": 100}},
        "voces": {"terms": {"field": "voces.keyword", "size": 500}},
        "tomo": {"terms": {"field": "tomo.keyword"}, "aggs": {"pagina": {"terms": {"field": "pagina.keyword"}}}},
        "anio": {
            "terms": {"script": {"source": year_script, "lang": "painless"}, "size": 3000},
            "aggs": {"mes": {"terms": {"script": {"source": month_script, "lang": "painless"}}}},
        },
        "mes": {"date_histogram": {"field": "fecha", "calendar_interval": "month", "order": {"_key": "desc"}}},
        "indices": {"terms": {"field": "_index"}},
    }


def _decode_cursor(cursor: str | None) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation="search")
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _cursor_state(cursor_payload: Mapping[str, Any]) -> JsonDict | None:
    raw = cursor_payload.get("raw")
    if not isinstance(raw, Mapping):
        return None
    body = raw.get("body")
    query = raw.get("query")
    if not isinstance(body, Mapping) or not isinstance(query, Mapping):
        return None
    return {
        "body": dict(body),
        "query": dict(query),
        "historico": raw.get("historico"),
        "solo_historico": raw.get("solo_historico"),
    }


def _cursor_offset(cursor_payload: Mapping[str, Any]) -> int:
    offset = cursor_payload.get("offset")
    if isinstance(offset, int) and offset >= 0:
        return offset
    return 0


def _next_cursor(
    *,
    body: Mapping[str, Any],
    query: Mapping[str, Any],
    historico: bool,
    solo_historico: bool,
    total: int | None,
    offset: int,
    limit: int,
    item_count: int,
) -> str | None:
    if item_count <= 0:
        return None
    next_offset = offset + item_count
    if total is not None and next_offset >= total:
        return None
    next_body = _json_copy(body)
    next_body["from"] = next_offset
    next_body["size"] = limit
    return make_cursor(
        source=SOURCE_ID,
        operation="search",
        offset=next_offset,
        limit=limit,
        raw={
            "query": dict(query),
            "body": next_body,
            "historico": historico,
            "solo_historico": solo_historico,
        },
    )


def _resolve_limit(
    args: argparse.Namespace,
    *,
    cursor_payload: Mapping[str, Any],
    body: Mapping[str, Any] | None,
) -> int:
    if getattr(args, "limit", None):
        return int(args.limit)
    cursor_limit = cursor_payload.get("limit")
    if isinstance(cursor_limit, int) and cursor_limit > 0:
        return cursor_limit
    body_size = body.get("size") if isinstance(body, Mapping) else None
    if isinstance(body_size, int) and body_size > 0:
        return body_size
    return DEFAULT_LIMIT


def _parse_raw_body(value: str | None) -> JsonDict:
    text = _optional_text(value)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise usage_error("--raw-body must be valid JSON", details={"json_error": str(exc)}) from exc
    if not isinstance(parsed, Mapping):
        raise usage_error("--raw-body must be a JSON object", details={"raw_body_type": type(parsed).__name__})
    return dict(parsed)


def _has_explicit_query_args(args: argparse.Namespace) -> bool:
    if getattr(args, "historico", None) is not None or getattr(args, "solo_historico", None) is not None:
        return True
    return any(
        _optional_text(getattr(args, name, None))
        for name in ("text", "tomo", "pagina", "numero", "expediente", "raw_body")
    )


def _response_query(
    query: Mapping[str, Any],
    *,
    historico: bool,
    solo_historico: bool,
    limit: int,
    offset: int,
) -> JsonDict:
    response = {key: value for key, value in query.items() if value is not None}
    response["historico"] = historico
    response["solo_historico"] = solo_historico
    response["limit"] = limit
    response["offset"] = offset
    return response


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(fetched_urls=[str(response.url)], raw={"status_code": response.status_code}),
        ) from exc


def _total_hits(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, Mapping):
        total = value.get("value")
        relation = _optional_text(value.get("relation"))
        return (_optional_int(total), relation)
    return _optional_int(value), None


def _legal_reference(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).replace(".", "")


def _snippet(highlight: Mapping[str, Any], *, attachments: Sequence[Mapping[str, Any]]) -> str | None:
    for field in TEXT_HIGHLIGHT_FIELDS:
        value = highlight.get(field)
        if isinstance(value, list):
            text = " ... ".join(str(item) for item in value if item)
        else:
            text = _optional_text(value)
        snippet = clean_snippet(text, max_length=SNIPPET_LENGTH) if text else None
        if snippet:
            return snippet
    for attachment in attachments:
        snippet = _optional_text(attachment.get("content_snippet"))
        if snippet:
            return snippet
    return None


def _solve_download_captcha(*, document_id: str, file_type: str, historical: bool) -> str:
    try:
        return solve_recaptcha_v3(HUMAN_URL, RECAPTCHA_SITEKEY, action=file_type)
    except CaptchaError as exc:
        raise LegalCliError(
            code="source_unavailable",
            message="PTN reCAPTCHA v3 solve failed",
            retryable=True,
            capability_required=CAPTCHA_SOLVER_CAPABILITY,
            details={
                "source": SOURCE_ID,
                "operation": "download",
                "id": document_id,
                "type": file_type,
                "historical": historical,
                "captcha_action": file_type,
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            provenance=_provenance(
                fetched_urls=[],
                source_response_id=f"download:{document_id}:{file_type}",
                raw={"captcha_action": file_type, "captcha_provider": "capsolver"},
            ),
        ) from exc


def _required_file_path(payload: Mapping[str, Any]) -> str:
    file_path = _optional_text(payload.get("file"))
    if not file_path:
        raise parse_error(
            "PTN confirmToken response is missing file",
            details={"payload_keys": sorted(str(key) for key in payload.keys())},
            provenance=_provenance(
                fetched_urls=[CONFIRM_TOKEN_URL],
                raw={"payload": dict(payload)},
            ),
        )
    return file_path


def _file_url(file_path: str) -> str:
    if file_path.startswith(("http://", "https://")):
        return file_path
    return f"{API_BASE_URL}/{file_path.lstrip('/')}"


def _pdf_headers() -> JsonDict:
    return {
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Referer": HUMAN_URL,
    }


def _historical_from_index(index: str | None) -> bool | None:
    if not index:
        return None
    normalized = index.casefold()
    if "histor" in normalized:
        return True
    if "dictamen" in normalized or "ptn" in normalized:
        return False
    return None


def _title(*, numero: str | None, tomo: str | None, pagina: str | None, expediente: str | None) -> str:
    parts: list[str] = []
    if numero:
        parts.append(numero)
    if tomo or pagina:
        location = " ".join(part for part in (f"Tomo {tomo}" if tomo else None, f"Pagina {pagina}" if pagina else None) if part)
        parts.append(location)
    if expediente:
        parts.append(expediente)
    return " | ".join(parts) if parts else "PTN dictamen"


def _bucket(bucket: Mapping[str, Any]) -> JsonDict:
    value = bucket.get("key_as_string", bucket.get("key"))
    return _compact(
        {
            "value": value,
            "label": _optional_text(value),
            "count": _optional_int(bucket.get("doc_count")),
        }
    )


def _tomo_bucket(bucket: Mapping[str, Any]) -> JsonDict:
    entry = _bucket(bucket)
    paginas = bucket.get("pagina")
    if isinstance(paginas, Mapping) and isinstance(paginas.get("buckets"), list):
        entry["paginas"] = [_bucket(item) for item in paginas["buckets"] if isinstance(item, Mapping)]
    return entry


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text for item in value if (text := _optional_text(item))]
    text = _optional_text(value)
    return [text] if text else []


def _unique(values: Sequence[str | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _required_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise parse_error(f"PTN search hit is missing {field}")
    return text


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


def _bool_arg(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "t", "yes", "y", "si", "sí"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _bool_or_default(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return _bool_arg(str(value))


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> JsonDict:
    merged = _json_copy(base)
    for key, value in override.items():
        if key == "query":
            merged[key] = _json_copy(value) if isinstance(value, Mapping | list) else value
        elif isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = _json_copy(value) if isinstance(value, Mapping | list) else value
    return merged


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


def _compact_error(error: Mapping[str, Any]) -> JsonDict:
    return _compact(
        {
            "type": error.get("type"),
            "reason": error.get("reason"),
            "root_cause": error.get("root_cause"),
        }
    )


def _useful_headers(response: httpx.Response) -> JsonDict:
    allowed = {
        "cache-control",
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "location",
        "retry-after",
    }
    return {key.lower(): value for key, value in response.headers.items() if key.lower() in allowed}


def _source_response_id(query: Mapping[str, Any], offset: int) -> str:
    text = _optional_text(query.get("text")) or "empty"
    return f"search:{offset}:{text[:120]}"


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
