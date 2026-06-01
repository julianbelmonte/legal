"""JUBA SCBA WebForms search adapter."""

from __future__ import annotations

import argparse
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from apps.legal.cache import SearchCacheRecord, load_search_state, save_search_state
from apps.legal.errors import not_found, parse_error, usage_error
from apps.legal.http import LegalHttpClient
from apps.legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from apps.legal.parsing import (
    HtmlNode,
    absolute_url,
    clean_snippet,
    clean_text,
    extract_select_options,
    normalize_date,
    parse_html,
)
from apps.legal.registry import get_source
from apps.legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "juba"
SOURCE_MAP = "apps/legal/docs/juba_scba.md"

BASE_URL = "https://juba.scba.gov.ar"
SEARCH_URL = f"{BASE_URL}/Buscar.aspx"
DETAIL_URL = f"{BASE_URL}/VerTextoCompleto.aspx"

TEXT_FIELD = "ctl00$cphMainContent$txtExpresionBusquedaRapida"
MATERIA_FIELD = "ctl00$cphMainContent$ddlMateria"
SUBMIT_FIELD = "ctl00$cphMainContent$btnUnicaBusqueda"
ANCHOR_FIELD = "ctl00$cphMainContent$Anclar"
PAGE_SELECT_FIELD = "ctl00$cphMainContent$ddlPaginaResultados"
EVENT_TARGET_FIELD = "__EVENTTARGET"
EVENT_ARGUMENT_FIELD = "__EVENTARGUMENT"
LAST_FOCUS_FIELD = "__LASTFOCUS"

NEXT_TARGET = "ctl00$cphMainContent$lnkSiguiente"
PREVIOUS_TARGET = "ctl00$cphMainContent$lnkAnterior"
FIRST_TARGET = "ctl00$cphMainContent$lnkInicio"
LAST_TARGET = "ctl00$cphMainContent$lnkFinal"
PAGE_TARGET = "ctl00$cphMainContent$lnkIrPagina"

DEFAULT_LIMIT = 10
DEFAULT_MATERIA = "Todos"
DEFAULT_BUCKET = "texto_sumario"
SNIPPET_LENGTH = 500
SOURCE_PAGE_SIZE = 20

_RESULT_RE = re.compile(
    r'<span[^>]+id="cphMainContent_RepeaterDatosResultados_lblCantidad_(?P<index>\d+)"[^>]*>'
    r"\s*Resultado:\s*(?P<number>[\d.]+)\s*de\s*(?P<total>[\d.]+)\s*</span>",
    re.IGNORECASE,
)
_H2_RE = re.compile(r"<h2\b[^>]*>(?P<text>.*?)</h2>", re.IGNORECASE | re.DOTALL)
_SUMARIO_ID_RE = re.compile(
    r'<td[^>]+class="[^"]*\btdFilaRepeaterDer\b[^"]*"[^>]*>\s*<p\b[^>]*>(?P<text>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
_MAIN_TEXT_RE = re.compile(
    r'<td[^>]+class="[^"]*\btdFilaRepeater\b[^"]*"[^>]*\bcolspan="2"[^>]*>\s*'
    r'<p\b[^>]*\btabindex="0"[^>]*>(?P<text>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
_REFERENCE_RE = re.compile(
    r'<span[^>]+id="lblReferenciaNormativa"[^>]*>(?P<text>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_BUCKET_COUNT_RE = re.compile(r"^(?P<label>.*?)\s*\((?P<count>[\d.]+)\)\s*$")
_POSTBACK_RE = re.compile(r"__doPostBack\(&#39;(?P<target>[^&]+)&#39;,\s*&#39;(?P<argument>[^&]*)&#39;\)")
_POSTBACK_PLAIN_RE = re.compile(r"__doPostBack\('(?P<target>[^']+)'\s*,\s*'(?P<argument>[^']*)'\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TEXT_COMPLETE_RE = re.compile(
    r"TEXTO\s+COMPLETO\s*</p>\s*</td>\s*</tr>\s*<tr>\s*<td[^>]*>(?P<body>.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)

PAGINATION_TARGETS: Mapping[str, str] = {
    "next": NEXT_TARGET,
    "previous": PREVIOUS_TARGET,
    "first": FIRST_TARGET,
    "last": LAST_TARGET,
}

DETAIL_SPAN_FIELDS: Mapping[str, str] = {
    "materia": "lblMateria",
    "tipo_de_fallo": "lblTipoDeFallo",
    "tribunal_emisor": "lblTribunalEmisor",
    "causa": "lblCausa",
    "fecha": "lblFecha",
    "nro_registro_interno": "lblNroregistroInterno",
    "caratula_publica": "lblCaratulaPublica",
    "magistrados_votantes": "lblmagistradosVotantes",
    "tribunal_origen": "lblTribunalOrigen",
    "nnf": "lblNNF",
    "observacion_fallo": "lblObservacionFallo",
    "sentencias_anuladas": "lblSentenciasAnuladas",
    "alcance": "lblAlcance",
    "iniciales": "lblIniciales",
    "observacion": "lblObservacion",
}


def _lookup_key(value: Any) -> str:
    text = clean_text(str(value)) if value is not None else ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = _NON_ALNUM_RE.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


@dataclass(frozen=True)
class BucketSpec:
    key: str
    anchor_id: str
    target: str
    label: str
    aliases: tuple[str, ...]


BUCKETS: tuple[BucketSpec, ...] = (
    BucketSpec(
        key="texto_sumario",
        anchor_id="cphMainContent_lnkResultadoTextoSumario",
        target="ctl00$cphMainContent$lnkResultadoTextoSumario",
        label="en Texto del Sumario",
        aliases=("texto_sumario", "sumario", "texto del sumario", "resumen"),
    ),
    BucketSpec(
        key="voces",
        anchor_id="cphMainContent_lnkResultadosVoces",
        target="ctl00$cphMainContent$lnkResultadosVoces",
        label="en voces",
        aliases=("voces", "voz", "en voces"),
    ),
    BucketSpec(
        key="texto_fallo",
        anchor_id="cphMainContent_lnkResultadoTextoFallo",
        target="ctl00$cphMainContent$lnkResultadoTextoFallo",
        label="en Texto del Fallo",
        aliases=("texto_fallo", "fallo", "texto del fallo", "fallos"),
    ),
    BucketSpec(
        key="busqueda_original",
        anchor_id="cphMainContent_lnkResultadoBusquedaOriginal",
        target="ctl00$cphMainContent$lnkResultadoBusquedaOriginal",
        label="búsqueda original",
        aliases=("busqueda_original", "original", "búsqueda original", "busqueda original"),
    ),
)

BUCKET_BY_KEY: Mapping[str, BucketSpec] = {bucket.key: bucket for bucket in BUCKETS}
BUCKET_BY_ANCHOR_ID: Mapping[str, BucketSpec] = {bucket.anchor_id: bucket for bucket in BUCKETS}
BUCKET_BY_TARGET: Mapping[str, BucketSpec] = {bucket.target: bucket for bucket in BUCKETS}
BUCKET_ALIASES: Mapping[str, BucketSpec] = {
    _lookup_key(alias): bucket
    for bucket in BUCKETS
    for alias in (bucket.key, bucket.label, *bucket.aliases)
}


@dataclass(frozen=True)
class JubaForm:
    url: str
    html: str
    form_action: str
    form_data: JsonDict
    hidden_fields: JsonDict
    materia_values: list[JsonDict]
    page_values: list[JsonDict]
    headers: JsonDict


@dataclass(frozen=True)
class JubaSearchPage:
    url: str
    html: str
    form_action: str
    form_data: JsonDict
    hidden_fields: JsonDict
    materia_values: list[JsonDict]
    page_values: list[JsonDict]
    buckets: list[JsonDict]
    active_bucket: str
    items: list[LegalItem]
    total: int | None
    first_result_number: int | None
    current_page: int
    page_index: int
    page_size: int
    headers: JsonDict


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text quick search")
    parser.add_argument("--materia", default=DEFAULT_MATERIA, help="JUBA materia value, e.g. Todos or Laboral")
    parser.add_argument("--bucket", help="bucket alias: sumario, voces, fallo")
    parser.add_argument("--page", type=_page_number, help="1-based source result page to fetch")


def add_buckets_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", "--q", dest="text", help="free text quick search")
    parser.add_argument("--materia", default=DEFAULT_MATERIA, help="JUBA materia value, e.g. Todos or Laboral")
    parser.add_argument("--bucket", help="switch to a bucket before returning counts")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id-fallo", dest="id_fallo", help="JUBA idFallo from a search result")


def add_next_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bucket", help="optional bucket alias to switch before continuing")
    parser.add_argument("--page", type=_page_number, help="1-based source result page to fetch")
    parser.add_argument(
        "--direction",
        choices=sorted(PAGINATION_TARGETS),
        default="next",
        help="WebForms continuation control to invoke",
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    if getattr(args, "cursor", None):
        raise usage_error("JUBA uses --search-id for WebForms continuation, not --cursor")
    limit = _resolve_limit(args)
    requested_bucket = bucket_from_arg(getattr(args, "bucket", None))
    requested_page = getattr(args, "page", None)

    if getattr(args, "search_id", None):
        return _handle_stateful_search(
            args,
            limit=limit,
            requested_bucket=requested_bucket,
            requested_page=requested_page,
        )

    query = query_from_args(args, bucket=requested_bucket)
    with _make_client() as client:
        form = fetch_form(client=client)
        search_page, fetched_urls = run_initial_search(
            client=client,
            form=form,
            query=query,
            bucket=requested_bucket,
            page=requested_page,
            include_raw=bool(args.raw),
        )
        items = search_page.items[:limit]
        search_id = _save_search_state(
            client=client,
            query=_state_query(query, active_bucket=search_page.active_bucket),
            limit=limit,
            search_page=search_page,
            returned_items=items,
            fetched_urls=fetched_urls,
        )

    return _search_response(
        operation="search",
        query=_response_query(query, search_page=search_page, limit=limit),
        items=items,
        search_page=search_page,
        search_id=search_id,
        fetched_urls=fetched_urls,
        raw={"search_id": search_id, "request": _request_evidence(search_page)},
    )


def handle_buckets(args: argparse.Namespace) -> LegalResponse:
    requested_bucket = bucket_from_arg(getattr(args, "bucket", None))
    limit = None

    if getattr(args, "search_id", None):
        search_page, query, fetched_urls, search_id = _page_from_state(
            args,
            limit=DEFAULT_LIMIT,
            requested_bucket=requested_bucket,
            requested_page=None,
        )
    else:
        query = query_from_args(args, bucket=requested_bucket)
        with _make_client() as client:
            form = fetch_form(client=client)
            search_page, fetched_urls = run_initial_search(
                client=client,
                form=form,
                query=query,
                bucket=requested_bucket,
                page=None,
                include_raw=bool(args.raw),
            )
            search_id = _save_search_state(
                client=client,
                query=_state_query(query, active_bucket=search_page.active_bucket),
                limit=DEFAULT_LIMIT,
                search_page=search_page,
                returned_items=[],
                fetched_urls=fetched_urls,
            )

    items = bucket_items(search_page.buckets, page_url=search_page.url)
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="buckets",
        query=_response_query(query, search_page=search_page, limit=limit),
        items=items,
        page=PageInfo(
            limit=len(items),
            offset=0,
            page=1,
            total=len(items),
            has_more=False,
            search_id=search_id,
        ),
        provenance=_provenance(
            fetched_urls=fetched_urls,
            source_response_id=_search_response_id(query, search_page=search_page),
            raw={"search_id": search_id, "request": _request_evidence(search_page)},
        ),
        facets=_facets(search_page),
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    id_fallo = _required_id_fallo(getattr(args, "id_fallo", None))
    url = detail_url(id_fallo)
    with _make_client() as client:
        response = client.request("GET", url)
    document = parse_detail_response(response, id_fallo=id_fallo, include_raw=bool(args.raw))
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request={"id_fallo": id_fallo},
        document=document,
        provenance=document.provenance,
    )


def handle_next(args: argparse.Namespace) -> LegalResponse:
    if getattr(args, "cursor", None):
        raise usage_error("JUBA uses --search-id for WebForms continuation, not --cursor")
    limit = _resolve_limit(args)
    requested_bucket = bucket_from_arg(getattr(args, "bucket", None))
    requested_page = getattr(args, "page", None)
    direction = _pagination_direction(getattr(args, "direction", "next"))
    search_id = _required_search_id(args.search_id, operation="next")
    record = load_search_state(search_id)
    if record is None:
        raise not_found("JUBA search state was not found or expired", details={"search_id": search_id})
    _validate_search_record(record)
    query = _query_from_record(record)
    cached_items = _cached_page_items(record)
    returned_count = _int_from_state(record.cursor_payload, "returned_count") or 0
    should_use_cached_items = (
        requested_bucket is None
        and requested_page is None
        and direction == "next"
        and returned_count < len(cached_items)
    )
    if should_use_cached_items:
        return _cached_next_response(
            record=record,
            query=query,
            limit=limit,
            items=cached_items[returned_count : returned_count + limit],
            new_returned_count=min(returned_count + limit, len(cached_items)),
        )

    form_data = _mapping_from_state(record.cursor_payload, "form_data")
    referer = _text_from_state(record.cursor_payload, "result_url") or SEARCH_URL
    action_url = _text_from_state(record.cursor_payload, "result_form_action") or SEARCH_URL
    active_bucket = _text_from_state(record.cursor_payload, "active_bucket") or _optional_text(query.get("bucket")) or DEFAULT_BUCKET
    fetched_urls: list[str] = [referer]

    with _make_client() as client:
        restore_cookies(client=client, cookies=record.cookies)
        search_page: JubaSearchPage | None = None
        switched_bucket = False
        if requested_bucket and requested_bucket != active_bucket:
            search_page = post_event_page(
                client=client,
                form_data=form_data,
                action_url=action_url,
                referer=referer,
                target=BUCKET_BY_KEY[requested_bucket].target,
                active_bucket=requested_bucket,
                include_raw=bool(args.raw),
            )
            fetched_urls.append(search_page.url)
            form_data = search_page.form_data
            referer = search_page.url
            action_url = search_page.form_action
            active_bucket = search_page.active_bucket
            switched_bucket = True

        should_post_pagination = requested_page is not None or direction != "next" or not switched_bucket
        if should_post_pagination:
            target = PAGE_TARGET if requested_page is not None else PAGINATION_TARGETS[direction]
            page_index = requested_page - 1 if requested_page is not None else None
            search_page = post_event_page(
                client=client,
                form_data=form_data,
                action_url=action_url,
                referer=referer,
                target=target,
                page_index=page_index,
                active_bucket=active_bucket,
                include_raw=bool(args.raw),
            )
            fetched_urls.append(search_page.url)
        if search_page is None:
            raise parse_error("cached JUBA search state did not produce a continuation page", details={"search_id": record.search_id})
        items = search_page.items[:limit]
        _save_search_state(
            client=client,
            query={**query, "bucket": search_page.active_bucket},
            limit=limit,
            search_page=search_page,
            returned_items=items,
            search_id=record.search_id,
            fetched_urls=fetched_urls,
        )

    response_query: JsonDict = {**query, "bucket": search_page.active_bucket, "limit": limit, "search_id": record.search_id}
    if requested_page is not None:
        response_query["page"] = requested_page
    if direction != "next":
        response_query["direction"] = direction
    return _search_response(
        operation="next",
        query=response_query,
        items=items,
        search_page=search_page,
        search_id=record.search_id,
        fetched_urls=fetched_urls,
        raw={"search_id": record.search_id, "request": _request_evidence(search_page)},
    )


def fetch_form(*, client: LegalHttpClient | None = None) -> JubaForm:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", SEARCH_URL)
        return parse_form_response(response)
    finally:
        if owns_client:
            http.close()


def parse_form_response(response: httpx.Response) -> JubaForm:
    html = response.text
    form_data, hidden_fields, form_action = extract_form_state(html, page_url=str(response.url))
    if "__VIEWSTATE" not in hidden_fields:
        raise parse_error(
            "JUBA search form did not include ASP.NET viewstate",
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(
                fetched_urls=[str(response.url)],
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(html, max_length=500)},
            ),
        )
    return JubaForm(
        url=str(response.url),
        html=html,
        form_action=form_action,
        form_data=form_data,
        hidden_fields=hidden_fields,
        materia_values=materia_values(html),
        page_values=page_values(html),
        headers=_useful_headers(response),
    )


def run_initial_search(
    *,
    client: LegalHttpClient,
    form: JubaForm,
    query: Mapping[str, Any],
    bucket: str | None,
    page: int | None,
    include_raw: bool = False,
) -> tuple[JubaSearchPage, list[str]]:
    form_data = search_form_data(form.form_data, query)
    response = client.request("POST", form.form_action, data=form_data, headers={"Referer": form.url})
    fetched_urls = [form.url, str(response.url)]
    search_page = parse_search_response(response, include_raw=include_raw)

    if bucket and search_page.active_bucket != bucket:
        search_page = post_event_page(
            client=client,
            form_data=search_page.form_data,
            action_url=search_page.form_action,
            referer=search_page.url,
            target=BUCKET_BY_KEY[bucket].target,
            active_bucket=bucket,
            include_raw=include_raw,
        )
        fetched_urls.append(search_page.url)

    if page and page > 1:
        search_page = post_event_page(
            client=client,
            form_data=search_page.form_data,
            action_url=search_page.form_action,
            referer=search_page.url,
            target=PAGE_TARGET,
            page_index=page - 1,
            active_bucket=search_page.active_bucket,
            include_raw=include_raw,
        )
        fetched_urls.append(search_page.url)

    return search_page, fetched_urls


def detail_url(id_fallo: str) -> str:
    return f"{DETAIL_URL}?{urlencode({'idFallo': id_fallo})}"


def parse_detail_response(
    response: httpx.Response,
    *,
    id_fallo: str,
    include_raw: bool = False,
) -> LegalDocument:
    html = response.text
    url = str(response.url)
    root = parse_html(html)
    metadata = detail_metadata(root)
    body = detail_body_text(html)
    if not body:
        raise parse_error(
            "JUBA detail page did not include full decision text",
            details={"id_fallo": id_fallo, "url": url, "status_code": response.status_code},
            provenance=_provenance(
                fetched_urls=[url],
                source_response_id=id_fallo,
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(html, max_length=500)},
            ),
        )

    title = _optional_text(metadata.get("caratula_publica")) or f"Fallo {id_fallo}"
    date_value = normalize_date(_optional_text(metadata.get("fecha")))
    document_type = _optional_text(metadata.get("tipo_de_fallo")) or "fallo"
    links = detail_links(root, page_url=url)
    raw: JsonDict = {"html": html} if include_raw else {}
    return LegalDocument(
        id=id_fallo,
        title=title,
        date=date_value,
        document_type=document_type,
        body=body,
        url=url,
        content_type=_optional_text(response.headers.get("content-type")),
        text_format="plain_text",
        metadata=_compact(
            {
                "materia": metadata.get("materia"),
                "tipo_de_fallo": metadata.get("tipo_de_fallo"),
                "tribunal_emisor": metadata.get("tribunal_emisor"),
                "causa": metadata.get("causa"),
                "fecha": metadata.get("fecha"),
                "nro_registro_interno": metadata.get("nro_registro_interno"),
                "caratula_publica": metadata.get("caratula_publica"),
                "magistrados_votantes": metadata.get("magistrados_votantes"),
                "tribunal_origen": metadata.get("tribunal_origen"),
                "nnf": metadata.get("nnf"),
                "observacion_fallo": metadata.get("observacion_fallo"),
                "sentencias_anuladas": metadata.get("sentencias_anuladas"),
                "alcance": metadata.get("alcance"),
                "iniciales": metadata.get("iniciales"),
                "observacion": metadata.get("observacion"),
            }
        ),
        links=links,
        source_fields=_compact(
            {
                "idFallo": id_fallo,
                "span_ids": metadata.get("span_ids"),
                "postback_actions": detail_postback_actions(root),
            }
        ),
        raw=raw,
        provenance=_provenance(
            fetched_urls=[url],
            source_response_id=id_fallo,
            raw={"headers": _useful_headers(response), "body_length": len(body)},
        ),
    )


def post_event_page(
    *,
    client: LegalHttpClient,
    form_data: Mapping[str, Any],
    action_url: str,
    referer: str,
    target: str,
    argument: str = "",
    page_index: int | None = None,
    active_bucket: str | None = None,
    include_raw: bool = False,
) -> JubaSearchPage:
    event_form = event_form_data(form_data, target=target, argument=argument, page_index=page_index)
    response = client.request("POST", action_url, data=event_form, headers={"Referer": referer})
    resolved_bucket = active_bucket or BUCKET_BY_TARGET.get(target, BUCKET_BY_KEY[DEFAULT_BUCKET]).key
    return parse_search_response(response, active_bucket=resolved_bucket, include_raw=include_raw)


def parse_search_response(
    response: httpx.Response,
    *,
    active_bucket: str | None = None,
    include_raw: bool = False,
) -> JubaSearchPage:
    html = response.text
    page_url = str(response.url)
    form_data, hidden_fields, form_action = extract_form_state(html, page_url=page_url)
    buckets = parse_buckets(html)
    parse_bucket = active_bucket or DEFAULT_BUCKET
    items, total, first_result_number = parse_result_items(
        html,
        page_url=page_url,
        active_bucket=parse_bucket,
        include_raw=include_raw,
    )
    page_opts = page_values(html)
    page_index = selected_page_index(page_opts, first_result_number=first_result_number)
    page_size = page_size_from_options(page_opts) or SOURCE_PAGE_SIZE
    current_page = page_index + 1
    inferred_bucket = active_bucket or infer_active_bucket(buckets=buckets, total=total)
    buckets = mark_active_bucket(buckets, active_bucket=inferred_bucket)

    return JubaSearchPage(
        url=page_url,
        html=html,
        form_action=form_action,
        form_data=form_data,
        hidden_fields=hidden_fields,
        materia_values=materia_values(html),
        page_values=page_opts,
        buckets=buckets,
        active_bucket=inferred_bucket,
        items=items,
        total=total,
        first_result_number=first_result_number,
        current_page=current_page,
        page_index=page_index,
        page_size=page_size,
        headers=_useful_headers(response),
    )


def search_form_data(form_data: Mapping[str, Any], query: Mapping[str, Any]) -> JsonDict:
    text = _required_text(query.get("text"), field="text")
    materia = _required_text(query.get("materia") or DEFAULT_MATERIA, field="materia")
    if materia == "-1":
        raise usage_error("JUBA search requires a materia other than -1")

    data = _postback_defaults(form_data)
    data[TEXT_FIELD] = text
    data[MATERIA_FIELD] = materia
    data[ANCHOR_FIELD] = "1"
    data[SUBMIT_FIELD] = "Buscar"
    data[EVENT_TARGET_FIELD] = ""
    data[EVENT_ARGUMENT_FIELD] = ""
    data[LAST_FOCUS_FIELD] = ""
    return data


def event_form_data(
    form_data: Mapping[str, Any],
    *,
    target: str,
    argument: str = "",
    page_index: int | None = None,
) -> JsonDict:
    data = _postback_defaults(form_data)
    data.pop(SUBMIT_FIELD, None)
    data[EVENT_TARGET_FIELD] = target
    data[EVENT_ARGUMENT_FIELD] = argument
    data[LAST_FOCUS_FIELD] = ""
    data[ANCHOR_FIELD] = data.get(ANCHOR_FIELD) or "1"
    if page_index is not None:
        if page_index < 0:
            raise usage_error("JUBA page must be greater than or equal to 1")
        data[PAGE_SELECT_FIELD] = str(page_index)
    return data


def extract_form_state(html: str, *, page_url: str) -> tuple[JsonDict, JsonDict, str]:
    root = parse_html(html)
    form_action = SEARCH_URL
    for form in root.iter("form"):
        form_action = absolute_url(page_url, form.get("action")) or page_url
        break

    data: JsonDict = {}
    hidden_fields: JsonDict = {}
    for input_node in root.iter("input"):
        name = _optional_text(input_node.get("name"))
        if not name:
            continue
        input_type = (input_node.get("type") or "text").lower()
        if input_type in {"submit", "button", "image", "reset"}:
            continue
        value = input_node.get("value") or ""
        data[name] = value
        if input_type == "hidden":
            hidden_fields[name] = value

    for select in root.iter("select"):
        name = _optional_text(select.get("name"))
        if not name:
            continue
        selected = selected_option_value(select)
        if selected is not None:
            data[name] = selected

    for field in (LAST_FOCUS_FIELD, EVENT_TARGET_FIELD, EVENT_ARGUMENT_FIELD):
        data.setdefault(field, "")
    return data, hidden_fields, form_action


def parse_buckets(html: str | HtmlNode) -> list[JsonDict]:
    root = html if isinstance(html, HtmlNode) else parse_html(html)
    buckets: list[JsonDict] = []
    for anchor in root.iter("a"):
        spec = BUCKET_BY_ANCHOR_ID.get(anchor.get("id") or "")
        if spec is None:
            continue
        text = anchor.text()
        parsed = _bucket_label_and_count(text)
        if parsed is None:
            continue
        label, count = parsed
        href = anchor.get("href") or ""
        target, argument = postback_from_href(href)
        buckets.append(
            {
                "key": spec.key,
                "label": label,
                "count": count,
                "target": target or spec.target,
                "argument": argument or "",
            }
        )
    return buckets


def parse_result_items(
    html: str,
    *,
    page_url: str,
    active_bucket: str | None = None,
    include_raw: bool = False,
) -> tuple[list[LegalItem], int | None, int | None]:
    matches = list(_RESULT_RE.finditer(html))
    if not matches:
        return [], None, None

    items: list[LegalItem] = []
    total = _digits_to_int(matches[0].group("total"))
    first_result_number = _digits_to_int(matches[0].group("number"))
    end_limit = _result_end_limit(html)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else end_limit
        fragment = html[match.start() : end]
        item = result_fragment_to_item(
            fragment,
            page_url=page_url,
            result_number=_digits_to_int(match.group("number")),
            result_total=_digits_to_int(match.group("total")),
            active_bucket=active_bucket,
            include_raw=include_raw,
        )
        if item is not None:
            items.append(item)
    return items, total, first_result_number


def result_fragment_to_item(
    fragment: str,
    *,
    page_url: str,
    result_number: int | None,
    result_total: int | None,
    active_bucket: str | None,
    include_raw: bool,
) -> LegalItem | None:
    root = parse_html(fragment)
    materia = _first_regex_text(_H2_RE, fragment)
    sumario_id = _first_regex_text(_SUMARIO_ID_RE, fragment)
    main_texts = [_clean_html_text(match.group("text")) for match in _MAIN_TEXT_RE.finditer(fragment)]
    main_texts = [text for text in main_texts if text]
    voces_text = main_texts[0] if main_texts else None
    sumario = main_texts[1] if len(main_texts) > 1 else None
    voces = split_voces(voces_text)
    reference = _first_regex_text(_REFERENCE_RE, fragment)
    fallos = extract_fallos(root, page_url=page_url)
    first_fallo_id = _optional_text(fallos[0].get("idFallo")) if fallos else None
    item_id = stable_result_id(sumario_id=sumario_id, first_fallo_id=first_fallo_id, result_number=result_number)
    if item_id is None:
        return None

    title = "; ".join(voces[:3]) if voces else None
    if not title and fallos:
        title = _optional_text(fallos[0].get("case_title")) or _optional_text(fallos[0].get("description"))
    title = title or item_id
    first_fallo_url = _optional_text(fallos[0].get("url")) if fallos else None
    first_fallo_date = _optional_text(fallos[0].get("date")) if fallos else None
    raw = {
        "result_number": result_number,
        "fragment": fragment,
    } if include_raw else {}
    source_fields = _compact(
        {
            "sumario_id": sumario_id,
            "result_number": result_number,
            "result_total": result_total,
            "materia": materia,
            "voces_text": voces_text,
            "voces": voces,
            "sumario": sumario,
            "referencia_normativa": reference,
            "fallos": fallos,
            "bucket": active_bucket,
        }
    )
    return LegalItem(
        id=item_id,
        title=title,
        date=first_fallo_date,
        document_type="sumario",
        url=first_fallo_url or SEARCH_URL,
        snippet=clean_snippet(sumario, max_length=SNIPPET_LENGTH),
        facets=_compact({"materia": materia, "bucket": active_bucket, "voces": voces}),
        source_fields=source_fields,
        raw=raw,
        provenance=_provenance(
            fetched_urls=[page_url],
            source_response_id=item_id,
            raw={"result_number": result_number, "idFallos": [fallo.get("idFallo") for fallo in fallos]},
        ),
    )


def extract_fallos(root: HtmlNode, *, page_url: str) -> list[JsonDict]:
    fallos: list[JsonDict] = []
    seen: set[str] = set()
    for anchor in root.iter("a"):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or "VerTextoCompleto.aspx" not in url:
            continue
        fallo_id = id_fallo_from_url(url)
        if not fallo_id or fallo_id in seen:
            continue
        seen.add(fallo_id)
        parent_text = _ancestor_text(anchor, "p")
        label = clean_text(anchor.text())
        description = _remove_link_label(parent_text, label)
        case_title = _case_title(description)
        fallo: JsonDict = {
            "idFallo": fallo_id,
            "url": url,
            "label": label or "Ver Texto Completo del Fallo",
        }
        if description:
            fallo["description"] = description
        date_value = normalize_date(description)
        if date_value:
            fallo["date"] = date_value
        if case_title:
            fallo["case_title"] = case_title
        fallos.append(fallo)
    return fallos


def bucket_items(buckets: Sequence[Mapping[str, Any]], *, page_url: str) -> list[LegalItem]:
    items: list[LegalItem] = []
    for bucket in buckets:
        key = _required_text(bucket.get("key"), field="bucket")
        label = _optional_text(bucket.get("label")) or key
        count = _int_or_none(bucket.get("count"))
        items.append(
            LegalItem(
                id=key,
                title=label,
                document_type="bucket",
                facets={"active": bool(bucket.get("active"))},
                source_fields={
                    "key": key,
                    "count": count,
                    "target": _optional_text(bucket.get("target")),
                    "argument": _optional_text(bucket.get("argument")) or "",
                },
                provenance=_provenance(
                    fetched_urls=[page_url],
                    source_response_id=f"bucket:{key}",
                    raw={"bucket": dict(bucket)},
                ),
            )
        )
    return items


def detail_metadata(root: HtmlNode) -> JsonDict:
    metadata: JsonDict = {}
    span_ids: list[str] = []
    for key, span_id in DETAIL_SPAN_FIELDS.items():
        value = span_text(root, span_id)
        if value:
            metadata[key] = value
        span_ids.append(span_id)
    metadata["span_ids"] = span_ids
    return metadata


def detail_body_text(html: str) -> str | None:
    match = _TEXT_COMPLETE_RE.search(html)
    if match:
        return _clean_detail_text(match.group("body"))

    root = parse_html(html)
    for node in root.iter("td"):
        if clean_text(node.text()) == "TEXTO COMPLETO":
            next_row = _next_sibling(node.parent, "tr") if node.parent is not None else None
            if next_row is None:
                continue
            body_cell = next((child for child in next_row.children if isinstance(child, HtmlNode) and child.tag == "td"), None)
            if body_cell is not None:
                return _clean_detail_text_from_node(body_cell)
    return None


def detail_links(root: HtmlNode, *, page_url: str) -> list[JsonDict]:
    links: list[JsonDict] = []
    seen: set[str] = set()
    for anchor in root.iter("a"):
        url = absolute_url(page_url, anchor.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        links.append(_compact({"url": url, "label": clean_text(anchor.text()), "id": _optional_text(anchor.get("id"))}))
    return links


def detail_postback_actions(root: HtmlNode) -> list[JsonDict]:
    actions: list[JsonDict] = []
    for anchor in root.iter("a"):
        target, argument = postback_from_href(anchor.get("href"))
        if target is None:
            continue
        actions.append(
            _compact(
                {
                    "id": _optional_text(anchor.get("id")),
                    "label": clean_text(anchor.text()),
                    "target": target,
                    "argument": argument or "",
                }
            )
        )
    return actions


def span_text(root: HtmlNode, span_id: str) -> str | None:
    for span in root.iter("span"):
        if span.get("id") == span_id:
            return clean_text(span.text())
    return None


def _handle_stateful_search(
    args: argparse.Namespace,
    *,
    limit: int,
    requested_bucket: str | None,
    requested_page: int | None,
) -> LegalResponse:
    search_page, query, fetched_urls, search_id = _page_from_state(
        args,
        limit=limit,
        requested_bucket=requested_bucket,
        requested_page=requested_page,
    )
    items = search_page.items[:limit]
    return _search_response(
        operation="search",
        query={**query, "limit": limit, "search_id": search_id},
        items=items,
        search_page=search_page,
        search_id=search_id,
        fetched_urls=fetched_urls,
        raw={"search_id": search_id, "request": _request_evidence(search_page)},
    )


def _page_from_state(
    args: argparse.Namespace,
    *,
    limit: int,
    requested_bucket: str | None,
    requested_page: int | None,
) -> tuple[JubaSearchPage, JsonDict, list[str], str]:
    search_id = _required_search_id(args.search_id, operation=getattr(args, "operation", "search"))
    record = load_search_state(search_id)
    if record is None:
        raise not_found("JUBA search state was not found or expired", details={"search_id": search_id})
    _validate_search_record(record)
    query = _query_from_record(record)
    form_data = _mapping_from_state(record.cursor_payload, "form_data")
    referer = _text_from_state(record.cursor_payload, "result_url") or SEARCH_URL
    action_url = _text_from_state(record.cursor_payload, "result_form_action") or SEARCH_URL
    target: str | None = None
    page_index: int | None = None
    active_bucket = _optional_text(query.get("bucket")) or _text_from_state(record.cursor_payload, "active_bucket")

    if requested_bucket and requested_bucket != active_bucket:
        target = BUCKET_BY_KEY[requested_bucket].target
        active_bucket = requested_bucket
    if requested_page and requested_page > 0:
        target = PAGE_TARGET
        page_index = requested_page - 1

    if target is None:
        cached_items = _cached_page_items(record)
        search_page = _search_page_from_record(record, items=cached_items)
        fetched_urls = _cached_fetched_urls(record)
    else:
        with _make_client() as client:
            restore_cookies(client=client, cookies=record.cookies)
            search_page = post_event_page(
                client=client,
                form_data=form_data,
                action_url=action_url,
                referer=referer,
                target=target,
                page_index=page_index,
                active_bucket=active_bucket,
                include_raw=bool(args.raw),
            )
            fetched_urls = [referer, search_page.url]
            _save_search_state(
                client=client,
                query={**query, "bucket": search_page.active_bucket},
                limit=limit,
                search_page=search_page,
                returned_items=[],
                search_id=record.search_id,
                fetched_urls=fetched_urls,
            )

    query = {**query, "bucket": search_page.active_bucket}
    if requested_page:
        query["page"] = requested_page
    return search_page, query, fetched_urls, record.search_id


def _search_response(
    *,
    operation: str,
    query: Mapping[str, Any],
    items: list[Any],
    search_page: JubaSearchPage,
    search_id: str,
    fetched_urls: list[str],
    raw: JsonDict,
) -> LegalResponse:
    offset = max((search_page.first_result_number or 1) - 1, 0)
    returned = len(items)
    total = search_page.total
    has_more = returned < len(search_page.items)
    if total is not None:
        has_more = offset + returned < total
    return LegalResponse.search(
        source=SOURCE_ID,
        operation=operation,
        query=dict(query),
        items=items,
        page=PageInfo(
            limit=_int_or_none(query.get("limit")),
            offset=offset,
            page=search_page.current_page,
            total=total,
            has_more=has_more,
            search_id=search_id,
        ),
        provenance=_provenance(
            fetched_urls=fetched_urls,
            source_response_id=_search_response_id(query, search_page=search_page),
            raw=raw,
        ),
        facets=_facets(search_page),
    )


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
    current_page = _int_from_state(state, "current_page") or 1
    page_size = _int_from_state(state, "page_size") or SOURCE_PAGE_SIZE
    page_index = _int_from_state(state, "page_index") or max(current_page - 1, 0)
    total = _int_from_state(state, "total")
    page_offset = page_index * page_size
    has_more = new_returned_count < len(_cached_page_items(record))
    if total is not None:
        has_more = page_offset + new_returned_count < total

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="next",
        query={**query, "limit": limit, "search_id": record.search_id},
        items=items,
        page=PageInfo(
            limit=limit,
            offset=page_offset + new_returned_count - len(items),
            page=current_page,
            total=total,
            has_more=has_more,
            search_id=record.search_id,
        ),
        provenance=_provenance(
            fetched_urls=_cached_fetched_urls(record),
            raw={"from_cache": True, "search_id": record.search_id, "current_page": current_page},
        ),
        facets={"buckets": state.get("buckets") or []},
    )


def _save_search_state(
    *,
    client: LegalHttpClient,
    query: Mapping[str, Any],
    limit: int,
    search_page: JubaSearchPage,
    returned_items: Sequence[Any],
    fetched_urls: list[str],
    search_id: str | None = None,
) -> str:
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
            "form_data": search_page.form_data,
            "result_url": search_page.url,
            "result_form_action": search_page.form_action,
            "current_page": search_page.current_page,
            "page_index": search_page.page_index,
            "page_size": search_page.page_size,
            "total": search_page.total,
            "active_bucket": search_page.active_bucket,
            "buckets": search_page.buckets,
            "available_count": len(search_page.items),
            "available_items": [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in search_page.items],
            "returned_count": len(returned_items),
        },
        raw_provenance={
            "source_map": SOURCE_MAP,
            "fetched_urls": fetched_urls,
            "headers": {"result": search_page.headers},
            "hidden_field_names": sorted(search_page.hidden_fields),
        },
    )
    return record.search_id


def _search_page_from_record(record: SearchCacheRecord, *, items: list[JsonDict]) -> JubaSearchPage:
    state = record.cursor_payload
    return JubaSearchPage(
        url=_text_from_state(state, "result_url") or SEARCH_URL,
        html="",
        form_action=_text_from_state(state, "result_form_action") or SEARCH_URL,
        form_data=_mapping_from_state(state, "form_data"),
        hidden_fields=dict(record.hidden_fields),
        materia_values=[],
        page_values=[],
        buckets=list(state.get("buckets") or []),
        active_bucket=_text_from_state(state, "active_bucket") or DEFAULT_BUCKET,
        items=items,  # type: ignore[arg-type]
        total=_int_from_state(state, "total"),
        first_result_number=(_int_from_state(state, "page_index") or 0) * (_int_from_state(state, "page_size") or SOURCE_PAGE_SIZE) + 1,
        current_page=_int_from_state(state, "current_page") or 1,
        page_index=_int_from_state(state, "page_index") or 0,
        page_size=_int_from_state(state, "page_size") or SOURCE_PAGE_SIZE,
        headers=dict(record.raw_provenance.get("headers", {}).get("result", {}))
        if isinstance(record.raw_provenance.get("headers"), Mapping)
        else {},
    )


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


def query_from_args(args: argparse.Namespace, *, bucket: str | None = None) -> JsonDict:
    text = _required_text(getattr(args, "text", None), field="text")
    materia = _required_text(getattr(args, "materia", None) or DEFAULT_MATERIA, field="materia")
    query: JsonDict = {"text": text, "materia": materia}
    if bucket:
        query["bucket"] = bucket
    return query


def _query_from_record(record: SearchCacheRecord) -> JsonDict:
    query = record.cursor_payload.get("query")
    if not isinstance(query, Mapping):
        raise parse_error("cached JUBA search state is missing query metadata", details={"search_id": record.search_id})
    return {str(key): value for key, value in query.items() if value not in (None, "")}


def _state_query(query: Mapping[str, Any], *, active_bucket: str) -> JsonDict:
    return {**{str(key): value for key, value in query.items()}, "bucket": active_bucket}


def _response_query(
    query: Mapping[str, Any],
    *,
    search_page: JubaSearchPage,
    limit: int | None,
) -> JsonDict:
    payload: JsonDict = {str(key): value for key, value in query.items() if value not in (None, "")}
    payload["bucket"] = search_page.active_bucket
    if limit is not None:
        payload["limit"] = limit
    if search_page.current_page != 1:
        payload["page"] = search_page.current_page
    return payload


def _facets(search_page: JubaSearchPage) -> JsonDict:
    facets: JsonDict = {
        "buckets": search_page.buckets,
        "active_bucket": search_page.active_bucket,
    }
    if search_page.materia_values:
        facets["materias"] = search_page.materia_values
    return facets


def materia_values(html: str) -> list[JsonDict]:
    return [
        {
            "value": option.get("value"),
            "label": option.get("label"),
            "selected": option.get("selected"),
        }
        for option in extract_select_options(html, name=MATERIA_FIELD, select_id="ddlMateria")
    ]


def page_values(html: str) -> list[JsonDict]:
    return [
        {
            "value": option.get("value"),
            "label": option.get("label"),
            "selected": option.get("selected"),
        }
        for option in extract_select_options(html, name=PAGE_SELECT_FIELD, select_id="ddlPaginaResultados")
    ]


def selected_option_value(select: HtmlNode) -> str | None:
    first: str | None = None
    for option in select.iter("option"):
        label = option.text()
        value = option.get("value")
        normalized = value if value is not None else label
        if first is None:
            first = normalized or ""
        if "selected" in option.attrs:
            return normalized or ""
    return first


def selected_page_index(options: Sequence[Mapping[str, Any]], *, first_result_number: int | None) -> int:
    for option in options:
        if option.get("selected"):
            value = _int_or_none(option.get("value"))
            if value is not None:
                return value
    if first_result_number is not None:
        return max((first_result_number - 1) // SOURCE_PAGE_SIZE, 0)
    return 0


def page_size_from_options(options: Sequence[Mapping[str, Any]]) -> int | None:
    for option in options:
        label = _optional_text(option.get("label"))
        if not label:
            continue
        match = re.search(r"(?P<start>\d+)\s*-\s*(?P<end>\d+)", label)
        if not match:
            continue
        start = int(match.group("start"))
        end = int(match.group("end"))
        if end >= start:
            return end - start + 1
    return None


def mark_active_bucket(buckets: Sequence[Mapping[str, Any]], *, active_bucket: str) -> list[JsonDict]:
    marked: list[JsonDict] = []
    for bucket in buckets:
        item = dict(bucket)
        item["active"] = item.get("key") == active_bucket
        marked.append(item)
    return marked


def infer_active_bucket(*, buckets: Sequence[Mapping[str, Any]], total: int | None) -> str:
    if total is not None:
        matches = [
            _optional_text(bucket.get("key"))
            for bucket in buckets
            if _int_or_none(bucket.get("count")) == total and _optional_text(bucket.get("key"))
        ]
        if len(matches) == 1 and matches[0]:
            return matches[0]
    return DEFAULT_BUCKET


def bucket_from_arg(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    bucket = BUCKET_ALIASES.get(_lookup_key(text))
    if bucket is None:
        raise usage_error("unknown JUBA bucket", details={"bucket": text, "known_buckets": sorted(BUCKET_BY_KEY)})
    return bucket.key


def postback_from_href(href: str | None) -> tuple[str | None, str | None]:
    if not href:
        return None, None
    match = _POSTBACK_RE.search(href) or _POSTBACK_PLAIN_RE.search(href)
    if not match:
        return None, None
    return clean_text(match.group("target")), clean_text(match.group("argument")) or ""


def id_fallo_from_url(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("idFallo")
    if values and values[0]:
        return values[0]
    return None


def split_voces(value: str | None) -> list[str]:
    if not value:
        return []
    return [text for text in (clean_text(part) for part in value.split("|")) if text]


def stable_result_id(
    *,
    sumario_id: str | None,
    first_fallo_id: str | None,
    result_number: int | None,
) -> str | None:
    normalized_sumario = _optional_text(sumario_id)
    if normalized_sumario and _lookup_key(normalized_sumario) != "nomina":
        return normalized_sumario
    if first_fallo_id:
        return first_fallo_id
    if normalized_sumario:
        return normalized_sumario
    if result_number is not None:
        return f"juba-result-{result_number}"
    return None


def _first_regex_text(pattern: re.Pattern[str], value: str) -> str | None:
    match = pattern.search(value)
    if not match:
        return None
    return _clean_html_text(match.group("text"))


def _clean_html_text(value: str | None) -> str | None:
    return clean_snippet(value)


def _bucket_label_and_count(value: str | None) -> tuple[str, int | None] | None:
    text = clean_text(value)
    if not text:
        return None
    match = _BUCKET_COUNT_RE.match(text)
    if not match:
        return text, None
    return clean_text(match.group("label")) or text, _digits_to_int(match.group("count"))


def _result_end_limit(html: str) -> int:
    candidates = [
        index
        for marker in ('id="cphMainContent_panelIrAPagina"', 'id="cphMainContent_lnkInicio"', 'id="ddlPaginaResultados"')
        for index in [html.find(marker)]
        if index >= 0
    ]
    return min(candidates) if candidates else len(html)


def _ancestor_text(node: HtmlNode, tag: str) -> str | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return clean_text(current.text())
        current = current.parent
    return None


def _remove_link_label(text: str | None, label: str | None) -> str | None:
    cleaned = clean_text(text)
    if not cleaned:
        return None
    if label:
        cleaned = cleaned.replace(label, " ")
    cleaned = cleaned.replace("Ver el Texto Completo del Fallo", " ")
    return clean_text(cleaned)


def _case_title(description: str | None) -> str | None:
    if not description:
        return None
    match = re.search(
        r"Car[aá]tula:\s*(?P<title>.*?)(?:\s+Magistrados\b|\s+Observaci[oó]n\b|$)",
        description,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return clean_text(match.group("title"))


def _clean_detail_text(fragment: str | None) -> str | None:
    if not fragment:
        return None
    root = parse_html(fragment)
    return _clean_detail_text_from_node(root)


def _clean_detail_text_from_node(node: HtmlNode) -> str | None:
    paragraphs = [
        clean_text(paragraph.text())
        for paragraph in node.iter("p")
        if not _has_descendant_tag(paragraph, "p")
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        return "\n\n".join(paragraphs)
    return clean_snippet(node)


def _has_descendant_tag(node: HtmlNode, tag: str) -> bool:
    for child in node.children:
        if not isinstance(child, HtmlNode):
            continue
        if child.tag == tag or _has_descendant_tag(child, tag):
            return True
    return False


def _next_sibling(node: HtmlNode | None, tag: str) -> HtmlNode | None:
    if node is None or node.parent is None:
        return None
    seen_current = False
    for sibling in node.parent.children:
        if sibling is node:
            seen_current = True
            continue
        if not seen_current or not isinstance(sibling, HtmlNode):
            continue
        if sibling.tag == tag:
            return sibling
    return None


def _postback_defaults(form_data: Mapping[str, Any]) -> JsonDict:
    data = {str(key): "" if value is None else str(value) for key, value in form_data.items()}
    for field in (LAST_FOCUS_FIELD, EVENT_TARGET_FIELD, EVENT_ARGUMENT_FIELD):
        data.setdefault(field, "")
    return data


def _resolve_limit(args: argparse.Namespace) -> int:
    return int(args.limit or DEFAULT_LIMIT)


def _page_number(value: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if page < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return page


def _required_search_id(value: Any, *, operation: str) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error(f"JUBA {operation} requires --search-id")
    return text


def _required_id_fallo(value: Any) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error("JUBA get requires --id-fallo")
    return text


def _required_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if not text:
        raise usage_error(f"JUBA search requires --{field}")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _digits_to_int(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    return int(digits) if digits else None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return _digits_to_int(str(value))


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {str(key): item for key, item in value.items() if item not in (None, "", [], {})}


def _validate_search_record(record: SearchCacheRecord) -> None:
    if record.source != SOURCE_ID:
        raise usage_error(
            "search id belongs to a different source",
            details={"search_id": record.search_id, "source": record.source},
        )
    if not isinstance(record.cursor_payload, Mapping):
        raise parse_error("cached JUBA search state is malformed", details={"search_id": record.search_id})


def _mapping_from_state(state: Mapping[str, Any], key: str) -> JsonDict:
    value = state.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise parse_error("cached JUBA search state is malformed", details={"field": key})
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _text_from_state(state: Mapping[str, Any], key: str) -> str | None:
    return _optional_text(state.get(key))


def _int_from_state(state: Mapping[str, Any], key: str) -> int | None:
    return _int_or_none(state.get(key))


def _pagination_direction(value: Any) -> str:
    text = _optional_text(value) or "next"
    if text not in PAGINATION_TARGETS:
        raise usage_error("unknown JUBA pagination direction", details={"direction": text, "known_directions": sorted(PAGINATION_TARGETS)})
    return text


def _cached_page_items(record: SearchCacheRecord) -> list[JsonDict]:
    value = record.cursor_payload.get("available_items")
    if value is None:
        return []
    if not isinstance(value, list):
        raise parse_error("cached JUBA search state has malformed available items", details={"search_id": record.search_id})
    items: list[JsonDict] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise parse_error("cached JUBA search state has malformed available items", details={"search_id": record.search_id})
        items.append({str(key): item_value for key, item_value in item.items()})
    return items


def _cached_fetched_urls(record: SearchCacheRecord) -> list[str]:
    value = record.raw_provenance.get("fetched_urls")
    if isinstance(value, list):
        urls = [url for url in (_optional_text(item) for item in value) if url]
        if urls:
            return urls
    result_url = _text_from_state(record.cursor_payload, "result_url")
    return [result_url] if result_url else [SEARCH_URL]


def _search_response_id(query: Mapping[str, Any], *, search_page: JubaSearchPage) -> str:
    return ":".join(
        [
            _optional_text(query.get("text")) or "",
            _optional_text(query.get("materia")) or "",
            search_page.active_bucket,
            str(search_page.current_page),
        ]
    )


def _request_evidence(search_page: JubaSearchPage) -> JsonDict:
    return {
        "form_action": search_page.form_action,
        "hidden_field_names": sorted(search_page.hidden_fields),
        "hidden_field_lengths": {key: len(str(value)) for key, value in search_page.hidden_fields.items()},
        "active_bucket": search_page.active_bucket,
        "current_page": search_page.current_page,
        "page_index": search_page.page_index,
        "page_size": search_page.page_size,
        "result_headers": search_page.headers,
    }


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
        source_urls=[SEARCH_URL, DETAIL_URL],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Referer": SEARCH_URL})


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"{SOURCE_ID} source is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("search", handle_search, help="search JUBA WebForms", add_arguments=add_search_arguments)
    adapter.register_operation("buckets", handle_buckets, help="search JUBA bucket counts", add_arguments=add_buckets_arguments)
    adapter.register_operation("get", handle_get, help="fetch a full JUBA decision by idFallo", add_arguments=add_get_arguments)
    adapter.register_operation("next", handle_next, help="continue a cached JUBA WebForms search", add_arguments=add_next_arguments)
    return adapter


register_adapter(build_adapter(), replace=True)
