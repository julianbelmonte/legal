"""CSJN browser-backed adapter."""

from __future__ import annotations

import argparse
import re
from typing import Any

from legal import enrichment
from legal.browser import (
    BotBrowser,
    BrowserExhausted,
    BrowserRetry,
    run_with_botbrowser,
)
from legal.errors import usage_error
from legal.models import JsonDict, LegalDocument, LegalError, LegalItem, LegalResponse, PageInfo, Provenance
from legal.parsing import HtmlNode, parse_html
from legal.pdf import extract_text
from legal.registry import SOURCE_BY_ID
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "csjn"
SOURCE_MAP = "legal/docs/csjn_jurisprudencia.md"

BASE = "https://sjconsulta.csjn.gov.ar"
SJ_CONTEXT = BASE + "/sjconsulta"
FALLOS_URL = BASE + "/sjconsulta/fallos/consulta.html"
SUMARIOS_URL = BASE + "/sjconsulta/consultaSumarios/consulta.html"
DOC_URL = BASE + "/sjconsulta/documentos/verDocumentoByIdLinksJSP.html?idDocumento="
PDF_URL = BASE + "/sjconsulta/documentos/verDocumentoById.html?idDocumento="
SITEKEY = "6Lc9hfArAAAAANCZ9hMlXTx8j7hKz52W2tgwovXk"
TERMS_INDEX = {"todas": 0, "algunas": 1, "frase": 2, "cercanas": 3}

DEFAULT_RETRIES = 3
RESULT_POLL_ATTEMPTS = 24
RESULT_POLL_MS = 500
# Bound the reCAPTCHA-gated submit navigation so a stalled proxy exit fails fast
# and a fresh exit is tried, instead of hanging the Playwright-default ~30s.
SUBMIT_NAV_TIMEOUT_MS = 20000
SEARCH_NAVIGATION_GLOB = "**/buscar.html"
FALLOS_FIELDS = ("fecha", "expediente", "tomo", "autos", "materia")
VER_DOC_RE = re.compile(r"ver\s*\(\s*['\"]?(?P<doc_id>\d+)['\"]?\s*\)")
ID_DOCUMENTO_RE = re.compile(r"idDocumento=(?P<doc_id>\d+)")


def add_fallos_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--texto", default="", help="free text to search in fallos")
    parser.add_argument("--partes", help="party-name filter")
    parser.add_argument("--fecha-desde", dest="fecha_desde", help="lower decision date bound")
    parser.add_argument("--fecha-hasta", dest="fecha_hasta", help="upper decision date bound")
    parser.add_argument(
        "--terms",
        default="todas",
        choices=tuple(TERMS_INDEX),
        help="CSJN term-matching mode",
    )
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=DEFAULT_RETRIES,
        help="search attempts; reCAPTCHA Enterprise scoring is probabilistic",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="show BotBrowser instead of running under the hidden display",
    )


def add_sumarios_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--texto", default="", help="free text to search in sumarios")
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=DEFAULT_RETRIES,
        help="search attempts; reCAPTCHA Enterprise scoring is probabilistic",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="show BotBrowser instead of running under the hidden display",
    )


def add_documento_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="id", help="CSJN idDocumento value")
    parser.add_argument(
        "--show",
        action="store_true",
        help="show BotBrowser instead of running under the hidden display",
    )


def add_download_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--id", dest="id", help="CSJN idDocumento value")
    enrichment.add_text_arguments(parser)


def handle_fallos(args: argparse.Namespace) -> LegalResponse:
    query = _query_from_args(args)
    records, meta = _run_fallos_search(args, hidden=not bool(args.show))

    limit = _optional_positive_int(getattr(args, "limit", None))
    response_records = records[:limit] if limit is not None else records
    items = [
        _record_to_item(record, fetched_url=_meta_url(meta), include_raw=bool(args.raw))
        for record in response_records
    ]
    total = _total_from_meta(meta) or len(records)
    ok = bool(records or meta.get("count") or meta.get("refine"))
    refine_warning = _refine_warning(meta, records)

    return LegalResponse(
        ok=ok,
        source=SOURCE_ID,
        operation="fallos",
        query={
            **query,
            "limit": limit,
            "count": len(records),
            "accepted": ok,
        },
        items=items,
        page=PageInfo(
            limit=limit,
            total=total,
            has_more=limit is not None and len(records) > len(response_records),
        ),
        provenance=_provenance(meta=meta, records=records),
        warnings=[refine_warning] if refine_warning else ([] if ok else [_meta_error(meta) or "csjn fallos search returned no accepted result"]),
        error=None
        if ok
        else LegalError(
            code="captcha_or_empty_result",
            message=_meta_error(meta) or "CSJN fallos search was not accepted",
            retryable=True,
            details={"meta": meta},
        ),
    )


def handle_sumarios(args: argparse.Namespace) -> LegalResponse:
    query = _sumarios_query_from_args(args)
    links, meta = _run_sumarios_search(args, hidden=not bool(args.show))

    limit = _optional_positive_int(getattr(args, "limit", None))
    response_links = links[:limit] if limit is not None else links
    items = [
        _sumario_link_to_item(link, fetched_url=_meta_url(meta, default_url=SUMARIOS_URL), include_raw=bool(args.raw))
        for link in response_links
    ]
    total = _total_from_meta(meta) or len(links)
    ok = bool(links or meta.get("count") or meta.get("refine"))
    refine_warning = _refine_warning(meta, links)

    return LegalResponse(
        ok=ok,
        source=SOURCE_ID,
        operation="sumarios",
        query={
            **query,
            "limit": limit,
            "count": len(links),
            "accepted": ok,
        },
        items=items,
        page=PageInfo(
            limit=limit,
            total=total,
            has_more=limit is not None and len(links) > len(response_links),
        ),
        provenance=_provenance(meta=meta, records=links, source_url=SUMARIOS_URL, operation="sumarios"),
        warnings=[refine_warning] if refine_warning else ([] if ok else [_meta_error(meta) or "csjn sumarios search returned no accepted result"]),
        error=None
        if ok
        else LegalError(
            code="captcha_or_empty_result",
            message=_meta_error(meta) or "CSJN sumarios search was not accepted",
            retryable=True,
            details={"meta": meta},
        ),
    )


def handle_documento(args: argparse.Namespace) -> LegalResponse:
    doc_id = _doc_id_from_args(args, operation="documento")
    doc_url = DOC_URL + doc_id
    pdf_url = PDF_URL + doc_id
    warnings: list[str] = []

    with BotBrowser(hidden=not bool(args.show)) as bb:
        page_response = bb.page.goto(doc_url, wait_until="domcontentloaded")
        page_status = getattr(page_response, "status", None)
        bb.page.wait_for_timeout(2500)
        title = _clean_text(bb.page.title())
        page_text = _page_body_text(bb.page)
        pdf = _fetch_pdf_via_context(bb.ctx, pdf_url)

    pdf_text = None
    if _has_pdf_bytes(pdf):
        try:
            pdf_text = _clean_text(extract_text(pdf["body"]))
        except Exception as exc:
            warnings.append(f"pdf text extraction failed: {type(exc).__name__}: {exc}")
    else:
        warnings.append("CSJN PDF bytes were not available from the browser context")

    body = pdf_text or page_text
    document = LegalDocument(
        id=f"{SOURCE_ID}:documento:{doc_id}",
        title=title or f"CSJN documento {doc_id}",
        document_type="fallo",
        body=body,
        url=doc_url,
        file_url=pdf_url,
        content_type=_pdf_content_type(pdf) or "application/pdf" if _has_pdf_bytes(pdf) else None,
        text_format="plain_text" if body else None,
        metadata=_compact(
            {
                "document_id": doc_id,
                "page_status": page_status,
                "page_text": page_text,
                "pdf_status": pdf.get("status"),
                "pdf_bytes": _pdf_byte_count(pdf),
                "pdf_content_type": _pdf_content_type(pdf),
                "pdf_text": pdf_text,
            }
        ),
        links=[
            {"url": doc_url, "label": "document page", "kind": "html"},
            {"url": pdf_url, "label": "document PDF", "kind": "pdf"},
        ],
        files=[
            _compact(
                {
                    "url": pdf_url,
                    "label": "CSJN PDF",
                    "kind": "pdf",
                    "content_type": _pdf_content_type(pdf) or "application/pdf",
                    "bytes": _pdf_byte_count(pdf),
                }
            )
        ],
        source_fields={"document_id": doc_id},
        raw={"pdf_headers": pdf.get("headers")} if bool(args.raw) else {},
        provenance=_document_provenance(
            doc_id=doc_id,
            fetched_urls=[doc_url, _pdf_url(pdf, fallback=pdf_url)],
            raw={
                "page_status": page_status,
                "pdf": _pdf_raw_meta(pdf),
                "pdf_text_extracted": bool(pdf_text),
            },
        ),
    )
    ok = bool(page_text or pdf_text or _has_pdf_bytes(pdf))
    if not ok:
        return LegalResponse.error_response(
            source=SOURCE_ID,
            operation="documento",
            request={"id": doc_id},
            warnings=warnings,
            provenance=document.provenance,
            error=LegalError(
                code="document_unavailable",
                message="CSJN document page and PDF were not available",
                retryable=True,
                details={"page_status": page_status, "pdf": _pdf_raw_meta(pdf)},
            ),
        )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="documento",
        request={"id": doc_id},
        document=document,
        provenance=document.provenance,
        warnings=warnings,
    )


def handle_download(args: argparse.Namespace) -> LegalResponse:
    doc_id = _doc_id_from_args(args, operation="download")
    want_text = bool(getattr(args, "want_text", False))
    save_path = getattr(args, "save_pdf", None) or None
    doc_url = DOC_URL + doc_id
    pdf_url = PDF_URL + doc_id

    with BotBrowser(hidden=True) as bb:
        page_response = bb.page.goto(doc_url, wait_until="domcontentloaded")
        page_status = getattr(page_response, "status", None)
        bb.page.wait_for_timeout(1000)
        pdf = _fetch_pdf_via_context(bb.ctx, pdf_url)

    if not _has_pdf_bytes(pdf):
        return LegalResponse.error_response(
            source=SOURCE_ID,
            operation="download",
            request=_compact({"id": doc_id, "text": True if want_text else None, "save_pdf": save_path}),
            provenance=_document_provenance(
                doc_id=doc_id,
                fetched_urls=[doc_url, _pdf_url(pdf, fallback=pdf_url)],
                raw={"page_status": page_status, "pdf": _pdf_raw_meta(pdf)},
            ),
            error=LegalError(
                code="download_unavailable",
                message="CSJN PDF bytes were not available from the browser context",
                retryable=True,
                details={"page_status": page_status, "pdf": _pdf_raw_meta(pdf)},
            ),
        )

    enrichment_fields = enrichment.finalize_document(
        pdf["body"],
        want_text=want_text,
        save_path=save_path,
    )
    text_value = enrichment_fields.get("text")
    text = text_value if isinstance(text_value, str) and text_value.strip() else None
    content_type = _pdf_content_type(pdf) or "application/pdf"
    document = LegalDocument(
        id=f"{SOURCE_ID}:download:{doc_id}",
        title=f"CSJN documento {doc_id}",
        document_type="fallo",
        body=text,
        url=doc_url,
        file_url=pdf_url,
        content_type=content_type,
        text_format="plain_text" if text else None,
        metadata=_compact(
            {
                "document_id": doc_id,
                "page_status": page_status,
                "pdf_status": pdf.get("status"),
                "pdf_content_type": _pdf_content_type(pdf),
            }
        )
        | enrichment_fields,
        links=[
            {"url": doc_url, "label": "document page", "kind": "html"},
            {"url": pdf_url, "label": "document PDF", "kind": "pdf"},
        ],
        files=[
            _compact(
                {
                    "url": pdf_url,
                    "label": "CSJN PDF",
                    "kind": "pdf",
                    "content_type": content_type,
                    "bytes": enrichment_fields.get("pdf_bytes"),
                    "saved": enrichment_fields.get("saved"),
                }
            )
        ],
        source_fields={"document_id": doc_id},
        raw={"pdf_headers": pdf.get("headers")} if bool(args.raw) else {},
        provenance=_document_provenance(
            doc_id=doc_id,
            fetched_urls=[doc_url, _pdf_url(pdf, fallback=pdf_url)],
            raw={
                "page_status": page_status,
                "pdf": _pdf_raw_meta(pdf),
                "text_requested": want_text,
                "saved": enrichment_fields.get("saved"),
            },
        ),
    )
    return LegalResponse.document_response(
        source=SOURCE_ID,
        operation="download",
        request=_compact({"id": doc_id, "text": True if want_text else None, "save_pdf": save_path}),
        document=document,
        provenance=document.provenance,
    )


def _run_fallos_search(args: argparse.Namespace, *, hidden: bool = True) -> tuple[list[JsonDict], JsonDict]:
    """Run a CSJN fallos search, rotating the proxy exit on each retry.

    A single attempt drives the page's native reCAPTCHA submit path; soft
    failures (WAF status, navigation never completing, recaptcha rejected / no
    results) raise :class:`BrowserRetry` so ``run_with_botbrowser`` relaunches
    behind a fresh exit. On exhaustion the last failure ``meta`` is returned so
    the handler can surface a transparent, retryable envelope.
    """
    retries = _optional_positive_int(getattr(args, "retries", None)) or DEFAULT_RETRIES

    def _attempt(page: Any, index: int) -> tuple[list[JsonDict], JsonDict]:
        response = page.goto(FALLOS_URL, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        if status != 200:
            raise BrowserRetry({"error": f"WAF/HTTP {status}", "url": FALLOS_URL})

        page.wait_for_timeout(3000)
        _warmup_recaptcha(page)
        _fill_fallos_form(page, args)
        page.wait_for_timeout(600)

        try:
            _submit_and_wait(page, SEARCH_NAVIGATION_GLOB)
        except Exception:
            raise BrowserRetry({"error": "search navigation did not complete", "url": FALLOS_URL})

        records: list[JsonDict] = []
        meta: JsonDict = {}
        for _ in range(RESULT_POLL_ATTEMPTS):
            page.wait_for_timeout(RESULT_POLL_MS)
            records = _parse_fallos(page)
            meta = _result_meta(page)
            if records or meta.get("refine"):
                break

        if records or meta.get("count") or meta.get("refine"):
            for record in records:
                doc_id = _clean_text(record.get("doc_id"))
                if doc_id:
                    record["doc_id"] = doc_id
                    record["document_url"] = DOC_URL + doc_id
            meta["attempt"] = index
            return records, meta

        raise BrowserRetry({**meta, "error": meta.get("error") or "recaptcha rejected / no results", "url": FALLOS_URL})

    try:
        return run_with_botbrowser(_attempt, retries=retries, hidden=hidden)
    except BrowserExhausted as exhausted:
        return [], exhausted.meta


def _run_sumarios_search(args: argparse.Namespace, *, hidden: bool = True) -> tuple[list[JsonDict], JsonDict]:
    """Run a CSJN sumarios search, rotating the proxy exit on each retry.

    Same rotation contract as :func:`_run_fallos_search`: each retry runs behind
    a fresh proxy exit so a stalled/dead exit is abandoned rather than reused.
    """
    retries = _optional_positive_int(getattr(args, "retries", None)) or DEFAULT_RETRIES

    def _attempt(page: Any, index: int) -> tuple[list[JsonDict], JsonDict]:
        response = page.goto(SUMARIOS_URL, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        if status != 200:
            raise BrowserRetry({"error": f"WAF/HTTP {status}", "url": SUMARIOS_URL})

        page.wait_for_timeout(3000)
        _warmup_recaptcha(page)
        _fill_sumarios_form(page, args)
        page.wait_for_timeout(600)

        try:
            _submit_and_wait(page, SEARCH_NAVIGATION_GLOB)
        except Exception:
            raise BrowserRetry({"error": "search navigation did not complete", "url": SUMARIOS_URL})

        # Results and the count badge settle asynchronously after navigation;
        # poll until rows render (or the refine notice appears) before reading
        # the count, otherwise a stale loading total leaks into `total`.
        links: list[JsonDict] = []
        meta: JsonDict = {}
        for _ in range(RESULT_POLL_ATTEMPTS):
            page.wait_for_timeout(RESULT_POLL_MS)
            links = _parse_sumario_links(page)
            meta = _result_meta(page, kind="sumarios")
            if links or meta.get("refine"):
                break
        page_text = _page_body_text(page, max_chars=4000)
        meta.update({"attempt": index, "page_text": page_text})
        if links or meta.get("count") or meta.get("refine"):
            return links, meta

        raise BrowserRetry({**meta, "error": meta.get("error") or "recaptcha rejected / no results", "url": SUMARIOS_URL})

    try:
        return run_with_botbrowser(_attempt, retries=retries, hidden=hidden)
    except BrowserExhausted as exhausted:
        return [], exhausted.meta


def _warmup_recaptcha(page: Any) -> None:
    """Pre-execute Enterprise reCAPTCHA and add small human-like pointer motion."""
    page.evaluate(
        """async (sk) => {
            await new Promise(r => grecaptcha.enterprise.ready(r));
            try { await grecaptcha.enterprise.execute(sk, {action: 'submit'}); } catch (e) {}
        }""",
        SITEKEY,
    )
    page.mouse.move(380, 280)
    page.mouse.move(560, 400)
    page.wait_for_timeout(400)


def _fill_sumarios_form(page: Any, args: argparse.Namespace, *, pace: int = 0) -> None:
    texto = _clean_text(getattr(args, "texto", None)) or ""
    selector = page.evaluate(
        """() => {
            const el=document.querySelector('[name="filter.fullText"]')
                   || document.querySelector('[name="texto"]')
                   || document.querySelector('input[type=text]');
            return el && el.getAttribute('name') ? '[name="'+el.getAttribute('name')+'"]' : null;
        }"""
    )
    if not selector:
        return
    try:
        page.query_selector(selector).click()
        page.type(selector, texto, delay=45 if pace else 35)
    except Exception:
        page.evaluate(
            """(query) => {
                const el=document.querySelector('[name="filter.fullText"]')
                       || document.querySelector('[name="texto"]')
                       || document.querySelector('input[type=text]');
                if(el){
                    el.value=query;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            texto,
        )


def parse_fallos_html(html: str) -> list[JsonDict]:
    root = parse_html(html)
    records: list[JsonDict] = []
    for row in root.iter():
        row_id = row.get("id") or ""
        if not row_id.startswith("fallosAnalisis"):
            continue
        record = {field: _class_text(row, field) for field in FALLOS_FIELDS}
        record["doc_id"] = _nearby_ver_doc_id(row)
        records.append(_normalize_record(record))
    return records


def _parse_fallos(page: Any) -> list[JsonDict]:
    try:
        return parse_fallos_html(page.content())
    except Exception:
        pass

    records = page.evaluate(
        """() => {
            const rows=[...document.querySelectorAll('[id^=fallosAnalisis]')];
            return rows.map(r => {
                const q=s=>{const e=r.querySelector(s);return e?e.innerText.trim():null;};
                const verA=r.parentElement?.querySelector('[onclick^="return ver("]')
                         || r.closest('tr')?.querySelector('[onclick^="return ver("]');
                let docId=null;
                if(verA){const m=(verA.getAttribute('onclick')||'').match(/ver\\((\\d+)\\)/); if(m) docId=m[1];}
                return {fecha:q('.fecha'), expediente:q('.expediente'), tomo:q('.tomo'),
                        autos:q('.autos'), materia:q('.materia'), doc_id:docId};
            });
        }"""
    )
    return [_normalize_record(record) for record in records if isinstance(record, dict)]


def _class_text(node: HtmlNode, class_name: str) -> str | None:
    match = _first_descendant_with_class(node, class_name)
    return match.text() if match else None


def _first_descendant_with_class(node: HtmlNode, class_name: str) -> HtmlNode | None:
    for candidate in node.iter():
        if _has_class(candidate, class_name):
            return candidate
    return None


def _has_class(node: HtmlNode, class_name: str) -> bool:
    return class_name in (node.get("class") or "").split()


def _nearby_ver_doc_id(row: HtmlNode) -> str | None:
    doc_id = _first_ver_doc_id(row)
    if doc_id:
        return doc_id

    tr = _ancestor_or_self(row, "tr")
    if tr is not None and tr is not row:
        doc_id = _first_ver_doc_id(tr)
        if doc_id:
            return doc_id

    doc_id = _sibling_ver_doc_id(row)
    if doc_id:
        return doc_id

    if row.parent is None:
        return None
    parent_doc_ids = _ver_doc_ids(row.parent)
    unique_doc_ids = list(dict.fromkeys(parent_doc_ids))
    return unique_doc_ids[0] if len(unique_doc_ids) == 1 else None


def _first_ver_doc_id(node: HtmlNode) -> str | None:
    for candidate in node.iter():
        doc_id = _ver_doc_id(candidate)
        if doc_id:
            return doc_id
    return None


def _ver_doc_ids(node: HtmlNode) -> list[str]:
    return [doc_id for candidate in node.iter() if (doc_id := _ver_doc_id(candidate))]


def _ver_doc_id(node: HtmlNode) -> str | None:
    for attr_name in ("onclick", "href"):
        value = node.get(attr_name)
        if not value:
            continue
        match = VER_DOC_RE.search(value) or ID_DOCUMENTO_RE.search(value)
        if match:
            return match.group("doc_id")
    return None


def _sibling_ver_doc_id(row: HtmlNode) -> str | None:
    if row.parent is None:
        return None
    siblings = [child for child in row.parent.children if isinstance(child, HtmlNode)]
    try:
        index = siblings.index(row)
    except ValueError:
        return None

    for sibling in siblings[index + 1 :]:
        if _is_fallos_row(sibling):
            break
        doc_id = _first_ver_doc_id(sibling)
        if doc_id:
            return doc_id

    for sibling in reversed(siblings[:index]):
        if _is_fallos_row(sibling):
            break
        doc_id = _first_ver_doc_id(sibling)
        if doc_id:
            return doc_id
    return None


def _is_fallos_row(node: HtmlNode) -> bool:
    return (node.get("id") or "").startswith("fallosAnalisis")


def _ancestor_or_self(node: HtmlNode, tag: str) -> HtmlNode | None:
    current: HtmlNode | None = node
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def _parse_sumario_links(page: Any) -> list[JsonDict]:
    links = page.evaluate(
        """() => {
            const idFrom = (value, pattern) => {
                if(!value) return null;
                const match = value.match(pattern);
                return match ? match[1] : null;
            };
            // Each sumario result row carries a verFallo("...idDocumento=N...") button
            // and a seleccionar(M) checkbox. Anchor on the verFallo button (one per
            // result) and climb to the row container to read its text and ids.
            const buttons=[...document.querySelectorAll('[onclick*="verFallo"]')];
            return buttons.slice(0, 50).map((b, index) => {
                const verRef=b.getAttribute('onclick') || '';
                let row=b;
                for(let i=0;i<8 && row;i++){
                    if(row.querySelector && row.querySelector('[onclick*="seleccionar"]')) break;
                    row=row.parentElement;
                }
                const selEl=row && row.querySelector ? row.querySelector('[onclick*="seleccionar"]') : null;
                const selRef=selEl ? (selEl.getAttribute('onclick') || '') : '';
                const text=((row || b).innerText || (row || b).textContent || '').trim();
                // verFallo links come in two forms:
                //   verDocumentoByIdLinksJSP.html?idDocumento=N      (full fallo)
                //   verDocumentoSumario.html?idDocumentoSumario=N    (sumario)
                const docHrefMatch=verRef.match(/['\"]([^'\"]*\\?(?:idDocumento|idDocumentoSumario)=\\d+[^'\"]*)['\"]/);
                const docHref=docHrefMatch ? docHrefMatch[1] : null;
                return {
                    index,
                    label: text.slice(0, 240) || null,
                    text: text.slice(0, 1000) || null,
                    ref: (verRef || selRef).slice(0, 240) || null,
                    onclick: verRef || null,
                    // docHref is app-relative; the canonical URL is rebuilt from
                    // it downstream by prepending the /sjconsulta context path.
                    href: docHref,
                    url: null,
                    doc_id: idFrom(verRef, /[?&]idDocumento=(\\d+)/),
                    id_documento_sumario: idFrom(verRef, /idDocumentoSumario=(\\d+)/),
                    id_analisis: idFrom(selRef, /seleccionar\\((\\d+)\\)/)
                };
            });
        }"""
    )
    return [_normalize_sumario_link(link) for link in links if isinstance(link, dict)]


def _submit_and_wait(page: Any, target_url_glob: str) -> None:
    """Click Buscar and wait for the delayed reCAPTCHA-gated navigation.

    Bounded tighter than the Playwright default so a stalled exit is abandoned
    quickly (``run_with_botbrowser`` then retries behind a fresh exit) instead of
    hanging ~30s per attempt and blowing past the agent-side timeout.
    """
    with page.expect_navigation(url=target_url_glob, wait_until="domcontentloaded", timeout=SUBMIT_NAV_TIMEOUT_MS):
        _click_buscar(page)


def _click_buscar(page: Any) -> None:
    page.evaluate(
        """() => {
            const b=[...document.querySelectorAll('button,a')].find(
                e => /buscar\\s*\\(/.test(e.getAttribute('onclick')||'')
                     || e.innerText.trim().toLowerCase()==='buscar');
            if(!b) throw new Error('buscar button not found');
            b.click();
        }"""
    )


def _fill_fallos_form(page: Any, args: argparse.Namespace, *, pace: int = 0) -> None:
    texto = _clean_text(getattr(args, "texto", None)) or ""
    page.query_selector('[name="texto"]').click()
    page.type('[name="texto"]', texto, delay=45 if pace else 35)
    if getattr(args, "partes", None):
        page.type('[name="partes"]', args.partes, delay=35)
    if getattr(args, "fecha_desde", None):
        page.fill('[name="fechaDesde"]', args.fecha_desde)
    if getattr(args, "fecha_hasta", None):
        page.fill('[name="fechaHasta"]', args.fecha_hasta)
    idx = TERMS_INDEX.get(getattr(args, "terms", "todas"), 0)
    page.evaluate(
        "(i)=>{const rs=document.querySelectorAll('[name=terminos]'); if(rs[i]) rs[i].checked=true;}",
        idx,
    )


def _result_meta(page: Any, *, kind: str = "fallos") -> JsonDict:
    # The sumarios results page also shows a "Fallos: N" cross-counter (matching
    # fallos, a different tab) next to its own "Total: N" listing count, so the
    # preferred label depends on which search produced the page.
    return page.evaluate(
        """(kind) => {
            const t=document.body.innerText;
            const own=kind==='sumarios' ? /Sumarios:\\s*\\d+/ : /Fallos:\\s*\\d+/;
            const m=t.match(own)
                  ||t.match(/Total:\\s*\\d+/i)
                  ||t.match(/Mostrando\\s+\\d+\\s+a\\s+\\d+\\s+de\\s+\\d+/i);
            const refine=t.match(/arroja\\s+\\d+\\s+resultados[^\\n]*/i);
            return {count:m?m[0]:null, refine:refine?refine[0]:null, url:location.href};
        }""",
        kind,
    )


def _record_to_item(record: JsonDict, *, fetched_url: str, include_raw: bool = False) -> LegalItem:
    doc_id = _clean_text(record.get("doc_id"))
    expediente = _clean_text(record.get("expediente"))
    title = _clean_text(record.get("autos")) or expediente or doc_id or "CSJN fallo"
    item_id = doc_id or _fallback_item_id(record)
    document_url = _clean_text(record.get("document_url"))
    source_fields = {
        "fecha": _clean_text(record.get("fecha")),
        "expediente": expediente,
        "tomo": _clean_text(record.get("tomo")),
        "autos": _clean_text(record.get("autos")),
        "materia": _clean_text(record.get("materia")),
        "doc_id": doc_id,
        "document_url": document_url,
    }
    return LegalItem(
        id=item_id,
        title=title,
        date=_clean_text(record.get("fecha")),
        document_type="fallo",
        url=document_url,
        facets={
            "expediente": expediente,
            "tomo": _clean_text(record.get("tomo")),
            "materia": _clean_text(record.get("materia")),
        },
        source_fields=source_fields,
        raw=dict(record) if include_raw else {},
        provenance=Provenance.now(
            source_urls=[FALLOS_URL],
            fetched_urls=[fetched_url],
            source_map=SOURCE_MAP,
            source_response_id=doc_id,
        ),
    )


def _sumario_link_to_item(link: JsonDict, *, fetched_url: str, include_raw: bool = False) -> LegalItem:
    doc_id = _clean_text(link.get("doc_id"))
    sumario_id = _clean_text(link.get("id_documento_sumario"))
    analysis_id = _clean_text(link.get("id_analisis"))
    label = _clean_text(link.get("label")) or _clean_text(link.get("text"))
    ref = _clean_text(link.get("ref"))
    href = _clean_text(link.get("href"))
    url = _clean_text(link.get("url")) or (DOC_URL + doc_id if doc_id else None)
    item_id = doc_id or sumario_id or analysis_id or _fallback_sumario_id(link)
    source_fields = {
        "doc_id": doc_id,
        "id_documento_sumario": sumario_id,
        "id_analisis": analysis_id,
        "label": label,
        "ref": ref,
        "href": href,
        "onclick": _clean_text(link.get("onclick")),
        "url": url,
        "index": link.get("index"),
    }
    return LegalItem(
        id=item_id,
        title=label or doc_id or sumario_id or analysis_id or "CSJN sumario",
        document_type="sumario",
        url=url,
        snippet=label,
        facets={},
        source_fields=_compact(source_fields),
        raw=dict(link) if include_raw else {},
        provenance=Provenance.now(
            source_urls=[SUMARIOS_URL],
            fetched_urls=[fetched_url],
            source_map=SOURCE_MAP,
            source_response_id=doc_id or sumario_id or analysis_id or item_id,
        ),
    )


def _query_from_args(args: argparse.Namespace) -> JsonDict:
    return _compact(
        {
            "texto": _clean_text(getattr(args, "texto", None)) or "",
            "partes": _clean_text(getattr(args, "partes", None)),
            "fecha_desde": _clean_text(getattr(args, "fecha_desde", None)),
            "fecha_hasta": _clean_text(getattr(args, "fecha_hasta", None)),
            "terms": getattr(args, "terms", "todas"),
            "retries": _optional_positive_int(getattr(args, "retries", None)) or DEFAULT_RETRIES,
            "show": bool(getattr(args, "show", False)),
        }
    )


def _sumarios_query_from_args(args: argparse.Namespace) -> JsonDict:
    return _compact(
        {
            "texto": _clean_text(getattr(args, "texto", None)) or "",
            "retries": _optional_positive_int(getattr(args, "retries", None)) or DEFAULT_RETRIES,
            "show": bool(getattr(args, "show", False)),
        }
    )


def _normalize_record(record: dict[str, Any]) -> JsonDict:
    normalized = {
        "fecha": _clean_text(record.get("fecha")),
        "expediente": _clean_text(record.get("expediente")),
        "tomo": _clean_text(record.get("tomo")),
        "autos": _clean_text(record.get("autos")),
        "materia": _clean_text(record.get("materia")),
        "doc_id": _clean_text(record.get("doc_id")),
    }
    doc_id = normalized.get("doc_id")
    if doc_id:
        normalized["document_url"] = DOC_URL + doc_id
    return normalized


def _normalize_sumario_link(link: dict[str, Any]) -> JsonDict:
    href = _clean_text(link.get("href"))
    normalized = {
        "index": link.get("index"),
        "label": _clean_text(link.get("label")),
        "text": _clean_text(link.get("text")),
        "ref": _clean_text(link.get("ref")),
        "onclick": _clean_text(link.get("onclick")),
        "href": href,
        "url": _clean_text(link.get("url")),
        "doc_id": _clean_text(link.get("doc_id")),
        "id_documento_sumario": _clean_text(link.get("id_documento_sumario")),
        "id_analisis": _clean_text(link.get("id_analisis")),
    }
    if not normalized.get("url"):
        if href and href.startswith("/"):
            normalized["url"] = SJ_CONTEXT + href
        elif normalized.get("doc_id"):
            normalized["url"] = DOC_URL + normalized["doc_id"]
    return _compact(normalized)


def _provenance(
    *,
    meta: JsonDict,
    records: list[JsonDict],
    source_url: str = FALLOS_URL,
    operation: str = "fallos",
) -> Provenance:
    fetched_url = _meta_url(meta, default_url=source_url)
    raw = _compact(
        {
            "meta": meta,
            "count": len(records),
            "records": records,
        }
    )
    return Provenance.now(
        source_urls=[source_url],
        fetched_urls=[fetched_url],
        source_map=SOURCE_MAP,
        source_response_id=_source_response_id(meta, operation=operation),
        raw=raw,
    )


def _source_response_id(meta: JsonDict, *, operation: str = "fallos") -> str | None:
    attempt = meta.get("attempt")
    if attempt is None:
        return None
    return f"{operation}-attempt-{attempt}"


def _meta_url(meta: JsonDict, *, default_url: str = FALLOS_URL) -> str:
    return _clean_text(meta.get("url")) or default_url


def _meta_error(meta: JsonDict) -> str | None:
    return _clean_text(meta.get("error"))


def _refine_warning(meta: JsonDict, records: list[JsonDict]) -> str | None:
    """Surface CSJN's 'too many results, refine your query' state as guidance.

    CSJN's search backend returns zero result rows when a query matches more than
    its server cap (5000), instead emitting a 'arroja N resultados ... por favor
    refine la consulta' notice. Without surfacing this, an empty `items` list with
    `accepted: true` looks like a rejected captcha score. Tell the caller to narrow
    the query (add terms, restrict dates/court) so the result set drops under the cap.
    """
    if records:
        return None
    refine = _clean_text(meta.get("refine"))
    if not refine:
        return None
    return (
        f"CSJN returned no result rows because the query is too broad: {refine}. "
        "Narrow the search (add terms, restrict dates or court) so the result set "
        "falls under CSJN's server cap, then retry."
    )


def _total_from_meta(meta: JsonDict) -> int | None:
    for key in ("count", "refine"):
        value = _clean_text(meta.get(key))
        if not value:
            continue
        # "Mostrando 1 a 20 de 533" reports the total after "de"; otherwise the
        # first number is the count (e.g. "Total: 1", "Fallos: 12").
        mostrando = re.search(r"de\s+(\d+)\s*$", value, re.IGNORECASE)
        if mostrando:
            return int(mostrando.group(1))
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return None


def _fallback_item_id(record: JsonDict) -> str:
    parts = [
        _clean_text(record.get("fecha")),
        _clean_text(record.get("expediente")),
        _clean_text(record.get("tomo")),
        _clean_text(record.get("autos")),
    ]
    slug = "-".join(part for part in parts if part)
    slug = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", slug).strip("-")
    return slug[:120] or "csjn-fallo"


def _fallback_sumario_id(link: JsonDict) -> str:
    parts = [
        _clean_text(link.get("label")),
        _clean_text(link.get("id_analisis")),
        _clean_text(link.get("ref")),
        _clean_text(link.get("url")),
        _clean_text(link.get("index")),
    ]
    slug = "-".join(part for part in parts if part)
    slug = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", slug).strip("-")
    return slug[:120] or "csjn-sumario"


def _doc_id_from_args(args: argparse.Namespace, *, operation: str) -> str:
    doc_id = _clean_text(getattr(args, "id", None))
    if not doc_id:
        raise usage_error(
            f"{operation} requires --id",
            details={"source": SOURCE_ID, "operation": operation},
        )
    if not doc_id.isdigit():
        raise usage_error(
            "--id must be a numeric CSJN idDocumento",
            details={"source": SOURCE_ID, "operation": operation, "id": doc_id},
        )
    return doc_id


def _page_body_text(page: Any, *, max_chars: int | None = None) -> str | None:
    text = page.evaluate("() => document.body ? document.body.innerText : ''")
    cleaned = _clean_text(text)
    if cleaned is None or max_chars is None:
        return cleaned
    return cleaned[:max_chars]


def _fetch_pdf_via_context(ctx: Any, url: str) -> JsonDict:
    if ctx is None:
        return {"url": url, "error": "browser context is not available"}
    try:
        response = ctx.request.get(url)
        body = response.body()
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        return {
            "url": _clean_text(getattr(response, "url", None)) or url,
            "status": getattr(response, "status", None),
            "headers": headers,
            "content_type": _clean_text(headers.get("content-type")),
            "body": body,
        }
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


def _has_pdf_bytes(pdf: JsonDict) -> bool:
    body = pdf.get("body")
    if not isinstance(body, bytes) or not body:
        return False
    content_type = (_pdf_content_type(pdf) or "").lower()
    return "pdf" in content_type or body.startswith(b"%PDF")


def _pdf_byte_count(pdf: JsonDict) -> int | None:
    body = pdf.get("body")
    if isinstance(body, bytes):
        return len(body)
    return None


def _pdf_content_type(pdf: JsonDict) -> str | None:
    return _clean_text(pdf.get("content_type"))


def _pdf_url(pdf: JsonDict, *, fallback: str) -> str:
    return _clean_text(pdf.get("url")) or fallback


def _pdf_raw_meta(pdf: JsonDict) -> JsonDict:
    return _compact(
        {
            "url": _clean_text(pdf.get("url")),
            "status": pdf.get("status"),
            "content_type": _pdf_content_type(pdf),
            "pdf_bytes": _pdf_byte_count(pdf),
            "error": _clean_text(pdf.get("error")),
            "headers": pdf.get("headers"),
        }
    )


def _document_provenance(*, doc_id: str, fetched_urls: list[str], raw: JsonDict | None = None) -> Provenance:
    return Provenance.now(
        source_urls=[DOC_URL + doc_id, PDF_URL + doc_id],
        fetched_urls=fetched_urls,
        source_map=SOURCE_MAP,
        source_response_id=doc_id,
        raw=raw or {},
    )


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact(value: JsonDict) -> JsonDict:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def build_adapter() -> SourceAdapter:
    adapter = SourceAdapter(SOURCE_BY_ID[SOURCE_ID])
    adapter.register_operation(
        "fallos",
        handle_fallos,
        help="search CSJN fallos with BotBrowser",
        add_arguments=add_fallos_arguments,
    )
    adapter.register_operation(
        "sumarios",
        handle_sumarios,
        help="search CSJN sumarios with BotBrowser",
        add_arguments=add_sumarios_arguments,
    )
    adapter.register_operation(
        "documento",
        handle_documento,
        help="fetch a CSJN document page and extracted PDF text",
        add_arguments=add_documento_arguments,
    )
    adapter.register_operation(
        "download",
        handle_download,
        help="download a CSJN document PDF through BotBrowser",
        add_arguments=add_download_arguments,
    )
    return adapter


register_adapter(build_adapter(), replace=True)
