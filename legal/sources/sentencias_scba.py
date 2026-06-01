"""Sentencias SCBA direct lookup, search, and detail adapter."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx

from legal import enrichment
from legal.captcha import CaptchaError, solve_recaptcha_v3
from legal.errors import CAPTCHA_SOLVER_CAPABILITY, LegalCliError, parse_error, usage_error
from legal.http import LegalHttpClient
from legal.models import JsonDict, LegalDocument, LegalItem, LegalResponse, PageInfo, Provenance
from legal.pagination import decode_cursor, make_cursor
from legal.parsing import (
    HtmlNode,
    clean_snippet,
    clean_text,
    extract_links,
    extract_select_options,
    normalize_date,
    parse_html,
)
from legal.registry import get_source
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "sentencias-scba"
SOURCE_MAP = "apps/legal/docs/sentencias_scba.md"

BASE_URL = "https://sentencias.scba.gov.ar"
HUMAN_URL = f"{BASE_URL}/"
ORGANISMS_URL = f"{BASE_URL}/RegistroElectronico/OrganismosDeUnRegistro"
SEARCH_URL = f"{BASE_URL}/RegistroElectronico/BuscarRegistrosPorFechaYOrganismo"
DETAIL_URL = f"{BASE_URL}/RegistroElectronico/ObtenerRegistroVisualizar/"
PDF_URL = f"{BASE_URL}/RegistroElectronico/ObtenerRegistroVisualizarPdf/"
ANONYMIZE_URL = f"{BASE_URL}/RegistroElectronico/abrirAnomizar/"
RECAPTCHA_SITEKEY = "6LeF4iwqAAAAAPpCb51XDc_bKUk1PGtZnuOLah_0"
RECAPTCHA_PAGE_URL = HUMAN_URL
PDF_RECAPTCHA_ACTION = "btndescargar"
ANONYMIZE_RECAPTCHA_ACTION = "btnanomizar"

DEFAULT_LIMIT = 10
SNIPPET_LENGTH = 500
AJAX_HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Content-Type": "application/json;charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}
PROTECTED_JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": HUMAN_URL,
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass(frozen=True)
class Register:
    key: str
    id: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class OrganismPage:
    register: Register
    organisms: list[JsonDict]
    url: str
    headers: JsonDict


@dataclass(frozen=True)
class SearchPage:
    url: str
    html: str
    items: list[LegalItem]
    headers: JsonDict


@dataclass(frozen=True)
class Card:
    title: str
    key: str
    body: HtmlNode
    text: str


REGISTERS: tuple[Register, ...] = (
    Register(
        key="sentencias",
        id="1",
        label="REGISTRO DE SENTENCIAS",
        aliases=("1", "sentencia", "sentencias", "registro de sentencias", "registro sentencias", "rs"),
    ),
    Register(
        key="resoluciones",
        id="2",
        label="REGISTRO DE RESOLUCIONES",
        aliases=("2", "resolucion", "resoluciones", "registro de resoluciones", "registro resoluciones", "rr"),
    ),
)


def add_organisms_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--register", default="sentencias", help="register alias: sentencias/resoluciones or 1/2")


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--register", help="register alias: sentencias/resoluciones or 1/2")
    parser.add_argument("--organism", help="organism id or exact/unique organism name")
    parser.add_argument("--organism-id", dest="organism_id", help="organism id from the organisms operation")
    parser.add_argument("--organism-name", "--name", dest="organism_name", help="organism display name")
    parser.add_argument("--from", dest="date_from", help="date lower bound, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", help="date upper bound, YYYY-MM-DD")
    parser.add_argument("--text", "--q", dest="text", help="free text included in the source search")


def add_get_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("code", nargs="?", help="idCodigoAcceso returned by search")
    parser.add_argument("--code", dest="code_option", help="idCodigoAcceso returned by search")
    parser.add_argument("--id", dest="id", help="idCodigoAcceso returned by search")


def add_protected_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--code", dest="code_option", help="idCodigoAcceso returned by search")


def add_pdf_arguments(parser: argparse.ArgumentParser) -> None:
    add_protected_arguments(parser)
    enrichment.add_text_arguments(parser)


def add_anonymize_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--register", default="sentencias", help="register alias used to resolve nombreRegistro")
    parser.add_argument("--registro", dest="registro", help="literal nombreRegistro value sent to SCBA")
    parser.add_argument("--nro-registro", "--nro-reg", dest="nro_registro")
    parser.add_argument("--fecha", dest="fecha", help="fechaRegistro value")
    parser.add_argument("--nro-expediente", "--nro-exp", dest="nro_expediente")
    parser.add_argument("--caratula", dest="caratula")
    parser.add_argument("--organism", "--organism-name", dest="organism_name")


def handle_organisms(args: argparse.Namespace) -> LegalResponse:
    register = register_from_arg(getattr(args, "register", None))
    with _make_client() as client:
        page = fetch_organisms(register=register, client=client, include_raw=bool(args.raw))

    items = [organism_to_item(organism, register=register, fetched_url=page.url) for organism in page.organisms]
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="organisms",
        query=_register_query(register),
        items=items,
        page=PageInfo(limit=len(items), offset=0, page=1, total=len(items), has_more=False),
        provenance=_provenance(
            source_urls=[HUMAN_URL, ORGANISMS_URL],
            fetched_urls=[page.url],
            source_response_id=f"organisms:{register.id}",
            raw={"headers": page.headers, "register": _register_query(register), "count": len(items)},
        ),
        facets={"registers": [_register_query(item) for item in REGISTERS]},
    )


def handle_search(args: argparse.Namespace) -> LegalResponse:
    cursor_payload = _decode_cursor(getattr(args, "cursor", None), operation="search")
    if cursor_payload and not _has_explicit_search_args(args):
        query = _query_from_cursor(cursor_payload)
        offset = _cursor_offset(cursor_payload)
    else:
        query = query_from_args(args)
        offset = 0

    limit = _resolve_limit(args, cursor_payload=cursor_payload)
    fetched_urls: list[str] = []
    raw_headers: JsonDict = {}

    with _make_client() as client:
        query, organism_page = resolve_organism(query, client=client)
        if organism_page is not None:
            fetched_urls.append(organism_page.url)
            raw_headers["organisms"] = organism_page.headers
        body = search_body(query)
        response = client.request("POST", SEARCH_URL, json=body, headers=AJAX_HEADERS)
        search_page = parse_search_response(response, query=query, include_raw=bool(args.raw))

    fetched_urls.append(search_page.url)
    raw_headers["search"] = search_page.headers
    total = len(search_page.items)
    items = search_page.items[offset : offset + limit]
    next_cursor = _next_search_cursor(query=query, offset=offset, limit=limit, returned=len(items), total=total)

    return LegalResponse.search(
        source=SOURCE_ID,
        operation="search",
        query=_response_query(query, limit=limit, offset=offset),
        items=items,
        page=PageInfo(
            limit=limit,
            offset=offset,
            page=(offset // limit) + 1 if limit else 1,
            total=total,
            has_more=next_cursor is not None,
            next_cursor=next_cursor,
        ),
        provenance=_provenance(
            source_urls=[HUMAN_URL, ORGANISMS_URL, SEARCH_URL],
            fetched_urls=fetched_urls,
            source_response_id=_search_response_id(query),
            raw={
                "headers": raw_headers,
                "request_body": body,
                "returned_total": total,
                "sliced_count": len(items),
            },
        ),
        facets={
            "registers": [_register_query(register) for register in REGISTERS],
            "organism": _compact(
                {
                    "id": query.get("organism_id"),
                    "label": query.get("organism_name"),
                    "register_id": query.get("register_id"),
                }
            ),
        },
    )


def handle_get(args: argparse.Namespace) -> LegalResponse:
    code = _code_from_args(args, operation="get")
    request_body = {"idCodigoAcceso": code}
    with _make_client() as client:
        response = client.request("POST", DETAIL_URL, json=request_body, headers=AJAX_HEADERS)
        document = parse_detail_response(response, code=code, include_raw=bool(args.raw))

    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="get",
        request=request_body,
        document=document,
        provenance=document.provenance,
    )


def handle_pdf(args: argparse.Namespace) -> LegalResponse:
    code = _code_from_args(args, operation="pdf")
    want_text = bool(getattr(args, "want_text", False))
    save_path = getattr(args, "save_pdf", None) or None
    token = _solve_recaptcha(action=PDF_RECAPTCHA_ACTION, operation="pdf", source_response_id=f"pdf:{code}")
    request_body = {"idCodigoAcceso": code, "recaptchaToken": token}

    with _make_client() as client:
        response = client.request("POST", PDF_URL, json=request_body, headers=PROTECTED_JSON_HEADERS)
        payload = parse_pdf_response(response, code=code)

    pdf_data = _required_pdf_data(payload, response=response, code=code)
    pdf_bytes = _decode_pdf_data(pdf_data, response=response, code=code)
    enrichment_fields = enrichment.finalize_document(
        pdf_bytes,
        want_text=want_text,
        save_path=save_path,
    )
    text_value = enrichment_fields.get("text")
    text = text_value if isinstance(text_value, str) and text_value.strip() else None
    document = LegalDocument(
        id=f"{SOURCE_ID}:pdf:{code}",
        title=f"Sentencias SCBA PDF {code}",
        document_type="pdf",
        body=text,
        url=HUMAN_URL,
        file_url=PDF_URL,
        content_type="application/pdf",
        text_format="plain_text" if text else None,
        metadata=_compact(
            {
                "idCodigoAcceso": code,
                "success": payload.get("success"),
                "message": _optional_text(payload.get("message")),
            }
        )
        | enrichment_fields,
        files=[
            {
                "url": PDF_URL,
                "label": "Sentencias SCBA PDF",
                "kind": "pdf",
                "content_type": "application/pdf",
            }
        ],
        source_fields={
            "idCodigoAcceso": code,
            "pdf_endpoint": PDF_URL,
            "recaptcha_action": PDF_RECAPTCHA_ACTION,
        },
        raw={"response": _redact_pdf_payload(payload), "headers": _useful_headers(response)} if bool(args.raw) else {},
        provenance=_provenance(
            source_urls=[HUMAN_URL, PDF_URL],
            fetched_urls=[str(response.url)],
            source_response_id=f"pdf:{code}",
            raw={
                "headers": _useful_headers(response),
                "request_body": {"idCodigoAcceso": code, "recaptchaToken": "<redacted>"},
                "response_keys": sorted(str(key) for key in payload.keys()),
                "pdf_bytes": len(pdf_bytes),
                "captcha_action": PDF_RECAPTCHA_ACTION,
                "token_redacted": True,
            },
        ),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="pdf",
        request=_compact({"code": code, "text": True if want_text else None, "save_pdf": save_path}),
        document=document,
        provenance=document.provenance,
    )


def handle_anonymize(args: argparse.Namespace) -> LegalResponse:
    request_fields = anonymize_request_from_args(args)
    source_response_id = _anonymize_response_id(request_fields)
    token = _solve_recaptcha(
        action=ANONYMIZE_RECAPTCHA_ACTION,
        operation="anonymize",
        source_response_id=source_response_id,
    )
    request_body = dict(request_fields)
    request_body["recaptchaToken"] = token

    with _make_client() as client:
        response = client.request("POST", ANONYMIZE_URL, json=request_body, headers=PROTECTED_JSON_HEADERS)

    response_text = _response_text(response.text)
    document = LegalDocument(
        id=f"{SOURCE_ID}:anonymize:{source_response_id}",
        title=f"Sentencias SCBA anonymize {request_fields['nroReg']}",
        document_type="anonymize_response",
        body=response_text,
        url=HUMAN_URL,
        content_type=_optional_text(response.headers.get("content-type")) or "text/html",
        text_format="plain_text",
        metadata=request_fields,
        source_fields={
            "anonymize_endpoint": ANONYMIZE_URL,
            "recaptcha_action": ANONYMIZE_RECAPTCHA_ACTION,
        },
        raw={"response_text": response.text, "headers": _useful_headers(response)} if bool(args.raw) else {},
        provenance=_provenance(
            source_urls=[HUMAN_URL, ANONYMIZE_URL],
            fetched_urls=[str(response.url)],
            source_response_id=f"anonymize:{source_response_id}",
            raw={
                "headers": _useful_headers(response),
                "request_body": request_fields | {"recaptchaToken": "<redacted>"},
                "response_length": len(response.text),
                "captcha_action": ANONYMIZE_RECAPTCHA_ACTION,
                "token_redacted": True,
            },
        ),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="anonymize",
        request=request_fields,
        document=document,
        provenance=document.provenance,
    )


def fetch_organisms(
    *,
    register: Register,
    client: LegalHttpClient | None = None,
    include_raw: bool = False,
) -> OrganismPage:
    owns_client = client is None
    http = client or _make_client()
    try:
        response = http.request("GET", ORGANISMS_URL, params={"idRegistro": register.id})
        return parse_organisms_response(response, register=register, include_raw=include_raw)
    finally:
        if owns_client:
            http.close()


def parse_organisms_response(
    response: httpx.Response,
    *,
    register: Register,
    include_raw: bool = False,
) -> OrganismPage:
    options = _organism_options(response.text)
    organisms: list[JsonDict] = []
    for option in options:
        value = _optional_text(option.get("value"))
        label = _optional_text(option.get("label"))
        if not value or value in {"-1", "0"} or not label or _normalize_lookup(label).startswith("seleccione"):
            continue
        organism = {
            "id": value,
            "name": label,
            "register_id": register.id,
            "register": register.label,
        }
        if include_raw:
            organism["raw"] = dict(option)
        organisms.append(organism)

    if not organisms and "<option" not in response.text.lower():
        raise parse_error(
            "Sentencias SCBA organisms response did not include organism options",
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, ORGANISMS_URL],
                fetched_urls=[str(response.url)],
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(response.text, max_length=500)},
            ),
        )

    return OrganismPage(
        register=register,
        organisms=organisms,
        url=str(response.url),
        headers=_useful_headers(response),
    )


def organism_to_item(organism: Mapping[str, Any], *, register: Register, fetched_url: str) -> LegalItem:
    organism_id = _require_text(organism.get("id"), field="organism")
    name = _require_text(organism.get("name"), field="organism_name")
    source_fields = {
        "idOrganismo": organism_id,
        "nombreOrganismo": name,
        "idRegistro": register.id,
        "registro": register.label,
    }
    return LegalItem(
        id=organism_id,
        title=name,
        document_type="organism",
        facets={"register_id": register.id, "register": register.key},
        source_fields=source_fields,
        raw=dict(organism.get("raw") or {}),
        provenance=_provenance(
            source_urls=[HUMAN_URL, ORGANISMS_URL],
            fetched_urls=[fetched_url],
            source_response_id=f"organism:{register.id}:{organism_id}",
            raw={"register": _register_query(register)},
        ),
    )


def parse_search_response(
    response: httpx.Response,
    *,
    query: Mapping[str, Any],
    include_raw: bool = False,
) -> SearchPage:
    html = response.text
    fetched_url = str(response.url)
    if "grid-ListadoRegistros" not in html:
        raise parse_error(
            "Sentencias SCBA search response did not include the result table",
            details={"url": fetched_url, "status_code": response.status_code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, SEARCH_URL],
                fetched_urls=[fetched_url],
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(html, max_length=500)},
            ),
        )

    items: list[LegalItem] = []
    seen: set[str] = set()
    for row in _parse_search_rows(html):
        item = search_row_to_item(row, query=query, page_url=fetched_url, include_raw=include_raw)
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)

    return SearchPage(url=fetched_url, html=html, items=items, headers=_useful_headers(response))


def search_row_to_item(
    row: Mapping[str, Any],
    *,
    query: Mapping[str, Any],
    page_url: str,
    include_raw: bool = False,
) -> LegalItem | None:
    cells = _row_cells(row)
    if len(cells) < 5:
        return None

    code = _optional_text(row.get("code")) or _optional_text(cells[0])
    if not code or not re.fullmatch(r"[0-9A-Fa-f]{6,}", code):
        return None

    data_order = _row_data_order(row)
    register_number = _optional_text(row.get("nro_registro")) or _optional_text(cells[1])
    register_order = _optional_text(data_order[1]) if len(data_order) > 1 else None
    fecha = _optional_text(row.get("fecha")) or _optional_text(cells[2])
    date_value = normalize_date(data_order[2] if len(data_order) > 2 else None) or normalize_date(fecha)
    expediente = _optional_text(row.get("nro_expediente")) or _optional_text(cells[3])
    expediente_order = _optional_text(data_order[3]) if len(data_order) > 3 else None
    title = _optional_text(row.get("caratula")) or _optional_text(cells[4]) or code
    row_raw = {
        "cells": cells,
        "data_order": data_order,
        "record": {
            "code": code,
            "nro_registro": register_number,
            "fecha": fecha,
            "nro_expediente": expediente,
            "caratula": title,
        },
    }
    register_id = _optional_text(query.get("register_id"))
    register_label = _optional_text(query.get("register_label"))
    organism_id = _optional_text(query.get("organism_id"))
    organism_name = _optional_text(query.get("organism_name"))
    source_fields = _compact(
        {
            "idCodigoAcceso": code,
            "code": code,
            "idRegistro": register_id,
            "registro": register_label,
            "idOrganismo": organism_id,
            "nombreOrganismo": organism_name,
            "numeroRegistro": register_number,
            "nro_registro": register_number,
            "fecha": fecha,
            "registroOrden": register_order,
            "numeroExpediente": expediente,
            "nro_expediente": expediente,
            "caratula": title,
            "expedienteOrden": expediente_order,
            "detail_endpoint": DETAIL_URL,
            "pdf_endpoint": PDF_URL,
            "anonymize_endpoint": ANONYMIZE_URL,
        }
    )
    return LegalItem(
        id=code,
        title=title,
        date=date_value,
        document_type=_document_type(register_label or register_id),
        url=HUMAN_URL,
        snippet=clean_snippet(title, max_length=SNIPPET_LENGTH),
        facets=_compact(
            {
                "register_id": register_id,
                "register": register_label,
                "organism_id": organism_id,
                "organism": organism_name,
            }
        ),
        source_fields=source_fields,
        raw=row_raw if include_raw else {},
        provenance=_provenance(
            source_urls=[HUMAN_URL, SEARCH_URL],
            fetched_urls=[page_url],
            source_response_id=code,
            raw={"row": row_raw} if include_raw else {},
        ),
    )


def parse_detail_response(response: httpx.Response, *, code: str, include_raw: bool = False) -> LegalDocument:
    html = response.text
    fetched_url = str(response.url)
    root = parse_html(html)
    cards = _cards(root)
    document_card = _card_by_key(cards, "documento")
    body_text = document_card.text if document_card is not None else root.text()
    body_text = clean_text(body_text)
    if not body_text:
        raise parse_error(
            "Sentencias SCBA detail response did not include document text",
            details={"url": fetched_url, "status_code": response.status_code, "code": code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, DETAIL_URL],
                fetched_urls=[fetched_url],
                source_response_id=code,
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(html, max_length=500)},
            ),
        )

    metadata = detail_metadata(cards, code=code)
    links = extract_links(root, base_url=BASE_URL)
    files = [link for link in links if link.get("kind") in {"pdf", "document", "spreadsheet", "archive", "image"}]
    raw = {"html": html} if include_raw else {}
    document = LegalDocument(
        id=code,
        title=_optional_text(metadata.get("case_title")) or _first_body_line(body_text) or code,
        date=_optional_text(metadata.get("registration_date")) or _optional_text(metadata.get("signature_date")),
        document_type=_document_type(_optional_text(metadata.get("register"))),
        body=body_text,
        url=DETAIL_URL,
        content_type="text/html",
        text_format="plain_text",
        metadata=metadata,
        links=links,
        files=files,
        source_fields={
            "idCodigoAcceso": code,
            "card_titles": [card.title for card in cards],
            "html_length": len(html),
        },
        raw=raw,
        provenance=_provenance(
            source_urls=[HUMAN_URL, DETAIL_URL],
            fetched_urls=[fetched_url],
            source_response_id=code,
            raw={
                "headers": _useful_headers(response),
                "request_body": {"idCodigoAcceso": code},
                "card_titles": [card.title for card in cards],
                "body_length": len(body_text),
            },
        ),
    )
    return document


def detail_metadata(cards: Sequence[Card], *, code: str) -> JsonDict:
    expediente = _card_by_key(cards, "expediente")
    registracion = _card_by_key(cards, "registracion")
    firmantes = _card_by_key(cards, "firmantes")
    adjuntos = _card_by_key(cards, "documentos adjuntos")
    relacionadas = _card_by_key(cards, "relacionadas")

    metadata: JsonDict = {"idCodigoAcceso": code}
    if expediente is not None:
        exp_text = expediente.text
        exp_match = re.search(
            r"Organismo:\s*(?P<organism>.*?)\s+Causa:\s*(?P<case>.*?)\s+-\s+N[uú]mero:\s*(?P<number>\S+)",
            exp_text,
            flags=re.IGNORECASE,
        )
        if exp_match:
            metadata.update(
                _compact(
                    {
                        "organism": clean_text(exp_match.group("organism")),
                        "case_title": clean_text(exp_match.group("case")),
                        "case_number": clean_text(exp_match.group("number")),
                    }
                )
            )
        else:
            metadata["expediente_text"] = exp_text

    if registracion is not None:
        reg_text = registracion.text
        reg_match = re.search(
            r"Registro:\s*(?P<register>.*?)\s+-\s+N[uú]mero:\s*(?P<number>.*?)\s+-\s+"
            r"C[oó]digo acceso:\s*(?P<code>[A-Za-z0-9]+)(?:\s+-\s+(?P<visibility>[A-Z]+))?",
            reg_text,
            flags=re.IGNORECASE,
        )
        if reg_match:
            metadata.update(
                _compact(
                    {
                        "register": clean_text(reg_match.group("register")),
                        "register_number": clean_text(reg_match.group("number")),
                        "access_code": clean_text(reg_match.group("code")),
                        "visibility": clean_text(reg_match.group("visibility")),
                    }
                )
            )
        by_match = re.search(
            r"Registrado por:\s*(?P<by>.*?)\s+-\s+Fecha registraci[oó]n:\s*(?P<date>.*)$",
            reg_text,
            flags=re.IGNORECASE,
        )
        if by_match:
            registration_date_text = clean_text(by_match.group("date"))
            metadata.update(
                _compact(
                    {
                        "registered_by": clean_text(by_match.group("by")),
                        "registration_date_text": registration_date_text,
                        "registration_date": normalize_date(registration_date_text),
                    }
                )
            )
        if "register" not in metadata:
            metadata["registracion_text"] = reg_text

    if firmantes is not None:
        signatures = _signature_entries(firmantes.text)
        if signatures:
            metadata["signatures"] = signatures
            first_date = _optional_text(signatures[0].get("date"))
            normalized = normalize_date(first_date)
            if normalized:
                metadata["signature_date"] = normalized
        else:
            metadata["firmantes_text"] = firmantes.text

    if adjuntos is not None:
        metadata["attachments_text"] = adjuntos.text
        metadata["has_attachments"] = "no contiene" not in _normalize_lookup(adjuntos.text)
    if relacionadas is not None:
        metadata["related_text"] = relacionadas.text
        metadata["has_related"] = "no contiene" not in _normalize_lookup(relacionadas.text)

    return _compact(metadata)


def resolve_organism(query: Mapping[str, Any], *, client: LegalHttpClient) -> tuple[JsonDict, OrganismPage | None]:
    register = register_from_arg(query.get("register_id") or query.get("register"))
    organism_id = _optional_text(query.get("organism_id"))
    organism_name = _optional_text(query.get("organism_name"))
    if organism_id and organism_name:
        return dict(query), None

    organism_page = fetch_organisms(register=register, client=client)
    resolved = find_organism(organism_page.organisms, organism_id=organism_id, organism_name=organism_name)
    updated = dict(query)
    updated.update(
        {
            "organism_id": resolved["id"],
            "organism_name": resolved["name"],
            "register_id": register.id,
            "register": register.key,
            "register_label": register.label,
        }
    )
    return updated, organism_page


def find_organism(
    organisms: Sequence[Mapping[str, Any]],
    *,
    organism_id: str | None,
    organism_name: str | None,
) -> JsonDict:
    if organism_id:
        for organism in organisms:
            if _optional_text(organism.get("id")) == organism_id:
                return {"id": organism_id, "name": _require_text(organism.get("name"), field="organism_name")}
        raise usage_error(f"organism id {organism_id!r} was not found")

    lookup = _normalize_lookup(organism_name)
    if not lookup:
        raise usage_error("search requires --organism, --organism-id, or --organism-name")

    exact = [organism for organism in organisms if _normalize_lookup(organism.get("name")) == lookup]
    if len(exact) == 1:
        return {"id": _require_text(exact[0].get("id"), field="organism_id"), "name": _require_text(exact[0].get("name"), field="organism_name")}

    partial = [organism for organism in organisms if lookup in _normalize_lookup(organism.get("name"))]
    if len(partial) == 1:
        return {"id": _require_text(partial[0].get("id"), field="organism_id"), "name": _require_text(partial[0].get("name"), field="organism_name")}
    if len(partial) > 1:
        raise usage_error(
            "organism name is ambiguous; use --organism-id",
            details={
                "matches": [
                    {"id": organism.get("id"), "name": organism.get("name")}
                    for organism in partial[:20]
                ]
            },
        )
    raise usage_error(f"organism name {organism_name!r} was not found")


def query_from_args(args: argparse.Namespace) -> JsonDict:
    register = register_from_arg(getattr(args, "register", None))
    date_from = _iso_date_arg(getattr(args, "date_from", None), flag="from")
    date_to = _iso_date_arg(getattr(args, "date_to", None), flag="to")
    if not date_from or not date_to:
        raise usage_error("search requires --from and --to ISO dates")
    if date.fromisoformat(date_from) > date.fromisoformat(date_to):
        raise usage_error("--from must be before or equal to --to")

    organism_id = _optional_text(getattr(args, "organism_id", None))
    organism_name = _optional_text(getattr(args, "organism_name", None))
    organism = _optional_text(getattr(args, "organism", None))
    if organism and not organism_id and not organism_name:
        if organism.isdigit():
            organism_id = organism
        else:
            organism_name = organism

    if not organism_id and not organism_name:
        raise usage_error("search requires --organism, --organism-id, or --organism-name")

    return _compact(
        {
            "register": register.key,
            "register_id": register.id,
            "register_label": register.label,
            "organism_id": organism_id,
            "organism_name": organism_name,
            "date_from": date_from,
            "date_to": date_to,
            "text": _optional_text(getattr(args, "text", None)) or "",
        }
    )


def anonymize_request_from_args(args: argparse.Namespace) -> JsonDict:
    registro = _optional_text(getattr(args, "registro", None))
    if not registro:
        registro = register_from_arg(getattr(args, "register", None)).label
    return {
        "nroReg": _require_arg(args, "nro_registro", flag="nro-registro"),
        "fechaRegistro": _require_arg(args, "fecha", flag="fecha"),
        "nroExp": _require_arg(args, "nro_expediente", flag="nro-expediente"),
        "caratulaReg": _require_arg(args, "caratula", flag="caratula"),
        "nombreOrganismo": _require_arg(args, "organism_name", flag="organism"),
        "nombreRegistro": registro,
    }


def search_body(query: Mapping[str, Any]) -> JsonDict:
    return {
        "fDesde": _source_date(_require_text(query.get("date_from"), field="from")),
        "fHasta": _source_date(_require_text(query.get("date_to"), field="to")),
        "texoIncluido": _optional_text(query.get("text")) or "",
        "idOrganismo": _require_text(query.get("organism_id"), field="organism_id"),
        "idRegistro": _require_text(query.get("register_id"), field="register_id"),
        "nombreOrganismo": _require_text(query.get("organism_name"), field="organism_name"),
        "registro": _require_text(query.get("register_label"), field="register_label"),
    }


def register_from_arg(value: Any) -> Register:
    text = _optional_text(value) or "sentencias"
    lookup = _normalize_lookup(text)
    for register in REGISTERS:
        aliases = (*register.aliases, register.key, register.id, register.label)
        if lookup in {_normalize_lookup(alias) for alias in aliases}:
            return register
    raise usage_error(
        "--register must be one of sentencias, resoluciones, 1, or 2",
        details={"register": text},
    )


def build_adapter() -> SourceAdapter:
    source = get_source(SOURCE_ID)
    if source is None:
        raise RuntimeError(f"source {SOURCE_ID!r} is not registered")
    adapter = SourceAdapter(source)
    adapter.register_operation("organisms", handle_organisms, help="list Sentencias SCBA organisms", add_arguments=add_organisms_arguments)
    adapter.register_operation("search", handle_search, help="search Sentencias SCBA records", add_arguments=add_search_arguments)
    adapter.register_operation("get", handle_get, help="fetch Sentencias SCBA HTML detail", add_arguments=add_get_arguments)
    adapter.register_operation("pdf", handle_pdf, help="fetch Sentencias SCBA PDF with internal reCAPTCHA solving", add_arguments=add_pdf_arguments)
    adapter.register_operation("anonymize", handle_anonymize, help="send Sentencias SCBA anonymization request with internal reCAPTCHA solving", add_arguments=add_anonymize_arguments)
    return adapter


class _RowParser(HTMLParser):
    """Extract SCBA result-table rows, preserving hidden idCodigoAcceso cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[JsonDict] = []
        self._in_td = False
        self._cur: list[str] = []
        self._orders: list[str | None] = []
        self._buf: list[str] = []
        self._order: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized == "tr":
            self._cur = []
            self._orders = []
        elif normalized == "td":
            self._in_td = True
            self._buf = []
            self._order = _attr_value(attrs, "data-order")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "td" and self._in_td:
            self._in_td = False
            self._cur.append(clean_text(unescape("".join(self._buf))) or "")
            self._orders.append(clean_text(self._order))
            self._buf = []
            self._order = None
        elif normalized == "tr" and self._cur:
            self.rows.append({"cells": list(self._cur), "data_order": list(self._orders)})

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._buf.append(data)


def _organism_options(text: str) -> list[JsonDict]:
    options = extract_select_options(text, name="Organismos", select_id="Organismos")
    if options:
        return options

    root = parse_html(text)
    return [
        {
            "value": clean_text(option.get("value")) or clean_text(option.text()) or "",
            "label": clean_text(option.text()),
            "selected": "selected" in option.attrs,
            "disabled": "disabled" in option.attrs,
        }
        for option in root.iter("option")
    ]


def _parse_search_rows(text: str) -> list[JsonDict]:
    parser = _RowParser()
    parser.feed(text)
    parser.close()

    records: list[JsonDict] = []
    for row in parser.rows:
        cells = _row_cells(row)
        if len(cells) < 5 or not re.fullmatch(r"[0-9A-Fa-f]{6,}", cells[0] or ""):
            continue
        records.append(
            {
                "code": cells[0],
                "nro_registro": cells[1],
                "fecha": cells[2],
                "nro_expediente": cells[3],
                "caratula": cells[4],
                "cells": cells,
                "data_order": _row_data_order(row),
            }
        )
    return records


def _row_cells(row: Mapping[str, Any]) -> list[str]:
    cells = row.get("cells")
    if not isinstance(cells, Sequence) or isinstance(cells, str | bytes | bytearray):
        return []
    return [_optional_text(cell) or "" for cell in cells]


def _row_data_order(row: Mapping[str, Any]) -> list[str | None]:
    values = row.get("data_order")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        return []
    return [_optional_text(value) for value in values]


def _attr_value(attrs: Sequence[tuple[str, str | None]], name: str) -> str | None:
    normalized = name.lower()
    for key, value in attrs:
        if key.lower() == normalized:
            return value
    return None


def _search_rows(root: HtmlNode) -> list[HtmlNode]:
    table = next((node for node in root.iter("table") if node.get("id") == "grid-ListadoRegistros"), None)
    if table is None:
        return []
    tbody = next((node for node in table.iter("tbody") if _nearest_ancestor(node, "table") is table), None)
    container = tbody or table
    return [row for row in container.iter("tr") if _nearest_ancestor(row, "table") is table]


def _cards(root: HtmlNode) -> list[Card]:
    cards: list[Card] = []
    for node in root.iter("div"):
        if not _has_class(node, "card"):
            continue
        header = _first_descendant_with_class(node, "card-header")
        body = _first_descendant_with_class(node, "card-body")
        if header is None or body is None:
            continue
        title = _optional_text(header.text())
        text = _optional_text(body.text())
        if not title or not text:
            continue
        cards.append(Card(title=title, key=_normalize_lookup(title), body=body, text=text))
    return cards


def _card_by_key(cards: Sequence[Card], key: str) -> Card | None:
    normalized = _normalize_lookup(key)
    return next((card for card in cards if card.key == normalized), None)


def _first_descendant_with_class(node: HtmlNode, class_name: str) -> HtmlNode | None:
    return next((child for child in node.iter("div") if child is not node and _has_class(child, class_name)), None)


def _has_class(node: HtmlNode, class_name: str) -> bool:
    classes = (_optional_text(node.get("class")) or "").split()
    return class_name in classes


def _child_nodes(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _nearest_ancestor(node: HtmlNode, tag: str) -> HtmlNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _signature_entries(text: str) -> list[JsonDict]:
    entries: list[JsonDict] = []
    for match in re.finditer(
        r"Fecha:\s*(?P<date>.*?)\s+Funcionario:\s*(?P<official>.*?)(?:\s+---|$)",
        text,
        flags=re.IGNORECASE,
    ):
        entries.append(
            _compact(
                {
                    "date": clean_text(match.group("date")),
                    "official": clean_text(match.group("official")),
                    "date_iso": normalize_date(match.group("date")),
                }
            )
        )
    return entries


def _query_from_cursor(cursor_payload: Mapping[str, Any]) -> JsonDict:
    raw = cursor_payload.get("raw")
    query = raw.get("query") if isinstance(raw, Mapping) else None
    if not isinstance(query, Mapping):
        raise usage_error("invalid cursor", details={"cursor_error": "missing search query"})
    return dict(query)


def _has_explicit_search_args(args: argparse.Namespace) -> bool:
    for name in ("register", "organism", "organism_id", "organism_name", "date_from", "date_to", "text"):
        if _optional_text(getattr(args, name, None)):
            return True
    return False


def _decode_cursor(cursor: str | None, *, operation: str) -> JsonDict:
    if not cursor:
        return {}
    try:
        return decode_cursor(cursor, source=SOURCE_ID, operation=operation)
    except ValueError as exc:
        raise usage_error("invalid cursor", details={"cursor_error": str(exc)}) from exc


def _cursor_offset(cursor_payload: Mapping[str, Any]) -> int:
    offset = cursor_payload.get("offset")
    return offset if isinstance(offset, int) and offset >= 0 else 0


def _resolve_limit(args: argparse.Namespace, *, cursor_payload: Mapping[str, Any]) -> int:
    if getattr(args, "limit", None):
        return int(args.limit)
    cursor_limit = cursor_payload.get("limit")
    if isinstance(cursor_limit, int) and cursor_limit > 0:
        return cursor_limit
    return DEFAULT_LIMIT


def _next_search_cursor(
    *,
    query: Mapping[str, Any],
    offset: int,
    limit: int,
    returned: int,
    total: int,
) -> str | None:
    next_offset = offset + returned
    if returned <= 0 or next_offset >= total:
        return None
    return make_cursor(
        source=SOURCE_ID,
        operation="search",
        offset=next_offset,
        limit=limit,
        raw={"query": dict(query)},
    )


def _search_response_id(query: Mapping[str, Any]) -> str:
    parts = [
        "search",
        _optional_text(query.get("register_id")) or "",
        _optional_text(query.get("organism_id")) or "",
        _optional_text(query.get("date_from")) or "",
        _optional_text(query.get("date_to")) or "",
        _optional_text(query.get("text")) or "",
    ]
    return ":".join(parts)[:180]


def _response_query(query: Mapping[str, Any], *, limit: int, offset: int) -> JsonDict:
    response = _compact(dict(query))
    response["limit"] = limit
    response["offset"] = offset
    return response


def _register_query(register: Register) -> JsonDict:
    return {"register": register.key, "register_id": register.id, "register_label": register.label}


def _source_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}"


def _iso_date_arg(value: Any, *, flag: str) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise usage_error(f"--{flag} must be an ISO date YYYY-MM-DD") from exc


def _code_from_args(args: argparse.Namespace, *, operation: str) -> str:
    value = (
        _optional_text(getattr(args, "code_option", None))
        or _optional_text(getattr(args, "id", None))
        or _optional_text(getattr(args, "code", None))
    )
    if not value:
        raise usage_error(f"{operation} requires --code", details={"source": SOURCE_ID, "operation": operation})
    if not re.fullmatch(r"[A-Za-z0-9]+", value):
        raise usage_error("--code must be an idCodigoAcceso value")
    return value


def _solve_recaptcha(*, action: str, operation: str, source_response_id: str) -> str:
    try:
        return solve_recaptcha_v3(RECAPTCHA_PAGE_URL, RECAPTCHA_SITEKEY, action=action)
    except (CaptchaError, RuntimeError) as exc:
        raise LegalCliError(
            code="source_unavailable",
            message="Sentencias SCBA reCAPTCHA v3 solve failed",
            retryable=True,
            capability_required=CAPTCHA_SOLVER_CAPABILITY,
            details={
                "source": SOURCE_ID,
                "operation": operation,
                "captcha_action": action,
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            provenance=_provenance(
                source_urls=[HUMAN_URL],
                fetched_urls=[],
                source_response_id=source_response_id,
                raw={"captcha_action": action, "captcha_provider": "capsolver"},
            ),
        ) from exc


def _json_payload(response: httpx.Response, message: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise parse_error(
            message,
            details={"url": str(response.url), "status_code": response.status_code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, PDF_URL],
                fetched_urls=[str(response.url)],
                raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(response.text, max_length=500)},
            ),
        ) from exc


def parse_pdf_response(response: httpx.Response, *, code: str) -> JsonDict:
    payload = _json_payload(response, "Sentencias SCBA PDF response was not valid JSON")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise parse_error(
                "Sentencias SCBA PDF response string was not valid JSON",
                details={"url": str(response.url), "status_code": response.status_code, "code": code},
                provenance=_provenance(
                    source_urls=[HUMAN_URL, PDF_URL],
                    fetched_urls=[str(response.url)],
                    source_response_id=f"pdf:{code}",
                    raw={"headers": _useful_headers(response), "body_snippet": clean_snippet(payload, max_length=500)},
                ),
            ) from exc
    if not isinstance(payload, Mapping):
        raise parse_error(
            "Sentencias SCBA PDF payload must be a JSON object",
            details={"payload_type": type(payload).__name__, "code": code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, PDF_URL],
                fetched_urls=[str(response.url)],
                source_response_id=f"pdf:{code}",
            ),
        )

    data = dict(payload)
    if data.get("success") is not True:
        raise LegalCliError(
            code="source_unavailable",
            message=_optional_text(data.get("message")) or "Sentencias SCBA PDF request was rejected",
            retryable=True,
            details={"source": SOURCE_ID, "operation": "pdf", "code": code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, PDF_URL],
                fetched_urls=[str(response.url)],
                source_response_id=f"pdf:{code}",
                raw={"headers": _useful_headers(response), "response": _redact_pdf_payload(data)},
            ),
        )
    return data


def _required_pdf_data(payload: Mapping[str, Any], *, response: httpx.Response, code: str) -> str:
    pdf_data = payload.get("pdfData")
    if not isinstance(pdf_data, str) or not pdf_data:
        raise parse_error(
            "Sentencias SCBA PDF response is missing pdfData",
            details={"payload_keys": sorted(str(key) for key in payload.keys()), "code": code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, PDF_URL],
                fetched_urls=[str(response.url)],
                source_response_id=f"pdf:{code}",
                raw={"headers": _useful_headers(response), "response": _redact_pdf_payload(payload)},
            ),
        )
    return pdf_data


def _decode_pdf_data(pdf_data: str, *, response: httpx.Response, code: str) -> bytes:
    try:
        return base64.b64decode(pdf_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise parse_error(
            "Sentencias SCBA pdfData was not valid base64",
            details={"url": str(response.url), "status_code": response.status_code, "code": code},
            provenance=_provenance(
                source_urls=[HUMAN_URL, PDF_URL],
                fetched_urls=[str(response.url)],
                source_response_id=f"pdf:{code}",
                raw={"headers": _useful_headers(response), "pdfData_length": len(pdf_data)},
            ),
        ) from exc


def _redact_pdf_payload(payload: Mapping[str, Any]) -> JsonDict:
    redacted = dict(payload)
    if "pdfData" in redacted:
        value = redacted["pdfData"]
        redacted["pdfData"] = f"<base64:{len(value)} chars>" if isinstance(value, str) else "<redacted>"
    return redacted


def _response_text(text: str) -> str:
    try:
        cleaned = clean_text(parse_html(text).text())
    except Exception:
        cleaned = clean_text(re.sub(r"<[^>]+>", " ", text))
    return cleaned or text.strip()


def _anonymize_response_id(request_fields: Mapping[str, Any]) -> str:
    parts = [
        _optional_text(request_fields.get("nroReg")) or "",
        _optional_text(request_fields.get("fechaRegistro")) or "",
        _optional_text(request_fields.get("nroExp")) or "",
    ]
    return ":".join(parts)[:160]


def _require_arg(args: argparse.Namespace, name: str, *, flag: str) -> str:
    return _require_text(getattr(args, name, None), field=flag)


def _document_type(register: str | None) -> str:
    normalized = _normalize_lookup(register)
    if "resolucion" in normalized or register == "2":
        return "resolucion"
    if "sentencia" in normalized or register == "1":
        return "sentencia"
    return "sentencias_scba_record"


def _first_body_line(body: str) -> str | None:
    text = _optional_text(body)
    if not text:
        return None
    return clean_snippet(text, max_length=180)


def _normalize_lookup(value: Any) -> str:
    text = _optional_text(value)
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _require_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise usage_error(f"--{field} cannot be empty")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return clean_text(str(value))


def _compact(value: Mapping[str, Any]) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


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


def _provenance(
    *,
    source_urls: list[str],
    fetched_urls: list[str],
    source_response_id: str | None = None,
    raw: JsonDict | None = None,
) -> Provenance:
    return Provenance.now(
        source_urls=source_urls,
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=source_response_id,
        raw=raw or {},
    )


def _make_client() -> LegalHttpClient:
    return LegalHttpClient(headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7"})


register_adapter(build_adapter(), replace=True)
