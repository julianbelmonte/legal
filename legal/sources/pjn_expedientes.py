"""PJN expediente browser adapter substrate."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import re
from typing import Any

from legal.browser import BotBrowser
from legal.captcha import solve_image
from legal.errors import LegalCliError
from legal.models import LegalError, LegalItem, LegalResponse, Provenance
from legal.parsing import parse_html
from legal.registry import SOURCE_BY_ID
from legal.sources import SourceAdapter, register_adapter


SOURCE_ID = "pjn-expedientes"
SOURCE_MAP = "apps/legal/docs/pjn_expedientes.md"

HOME_URL = "https://scw.pjn.gov.ar/scw/home.seam"
CAPTCHA_FRAME_NAME = "captcha-frame"
CAPTCHA_FRAME_SELECTOR = 'iframe[name="captcha-frame"]'
DEFAULT_CAPTCHA_ATTEMPTS = 4

CAMARAS = {
    "0": "CSJ - Corte Suprema de Justicia de la Nacion",
    "1": "CIV - Camara Nacional de Apelaciones en lo Civil",
    "2": "CAF - Camara Nacional de Apelaciones en lo Contencioso Administrativo Federal",
    "3": "CCF - Camara Nacional de Apelaciones en lo Civil y Comercial Federal",
    "4": "CNE - Camara Nacional Electoral",
    "5": "CSS - Camara Federal de la Seguridad Social",
    "6": "CPE - Camara Nacional de Apelaciones en lo Penal Economico",
    "7": "CNT - Camara Nacional de Apelaciones del Trabajo",
    "8": "CFP - Camara Criminal y Correccional Federal",
    "9": "CCC - Camara Nacional de Apelaciones en lo Criminal y Correccional",
    "10": "COM - Camara Nacional de Apelaciones en lo Comercial",
    "11": "CPF - Camara Federal de Casacion Penal",
    "12": "CPN - Camara Nacional Casacion Penal",
    "13": "FBB - Justicia Federal de Bahia Blanca",
    "14": "FCR - Justicia Federal de Comodoro Rivadavia",
    "15": "FCB - Justicia Federal de Cordoba",
    "16": "FCT - Justicia Federal de Corrientes",
    "17": "FGR - Justicia Federal de General Roca",
    "18": "FLP - Justicia Federal de La Plata",
    "19": "FMP - Justicia Federal de Mar del Plata",
    "20": "FMZ - Justicia Federal de Mendoza",
    "21": "FPO - Justicia Federal de Posadas",
    "22": "FPA - Justicia Federal de Parana",
    "23": "FRE - Justicia Federal de Resistencia",
    "24": "FSA - Justicia Federal de Salta",
    "25": "FRO - Justicia Federal de Rosario",
    "26": "FSM - Justicia Federal de San Martin",
    "27": "FTU - Justicia Federal de Tucuman",
}


def handle_camaras(args: argparse.Namespace) -> LegalResponse:
    items = [
        LegalItem(
            id=f"{SOURCE_ID}:camara:{camara_id}",
            title=name,
            facets={"camara": camara_id},
            source_fields={"camara": camara_id, "name": name},
        )
        for camara_id, name in CAMARAS.items()
    ]
    return LegalResponse.search(
        source=SOURCE_ID,
        operation="camaras",
        query={},
        items=items,
        facets={
            "camaras": [
                {"id": camara_id, "name": name}
                for camara_id, name in CAMARAS.items()
            ]
        },
        provenance=_provenance(raw={"count": len(CAMARAS)}),
    )


def add_expediente_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--camara", help="jurisdiction id; see camaras")
    parser.add_argument("--numero", help="expediente number")
    parser.add_argument("--anio", help="expediente year")
    _add_browser_search_arguments(parser)


def add_parte_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--camara", help="jurisdiction id; see camaras")
    parser.add_argument("--role", help="party role, e.g. ACTOR or DEMANDADO")
    parser.add_argument("--parte", help="party name")
    _add_browser_search_arguments(parser)


def add_rh_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nombre", help="first name")
    parser.add_argument("--apellido", help="last name")
    _add_browser_search_arguments(parser)


def handle_expediente(args: argparse.Namespace) -> dict[str, Any]:
    def fill(page: Any) -> None:
        page.evaluate(
            """(v) => {
                const sel = document.querySelector(
                    '[id="formPublica:camaraNumAni"], select[name*="camaraNumAni"]'
                );
                if (sel) {
                    sel.value = v;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            str(args.camara),
        )
        page.wait_for_timeout(800)
        page.fill('[id="formPublica:numero"]', str(args.numero))
        page.fill('[id="formPublica:anio"]', str(args.anio))

    return _run_search(args, fill, '[id="formPublica:buscarPorNumeroButton"]')


def handle_parte(args: argparse.Namespace) -> dict[str, Any]:
    def fill(page: Any) -> None:
        _click_tab(page, "Por parte")
        page.evaluate(
            """(v) => {
                const sel = document.querySelector(
                    '[id="formPublica:camaraPartes"], select[name*="camaraPartes"]'
                );
                if (sel) {
                    sel.value = v;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            str(args.camara),
        )
        page.wait_for_timeout(2000)
        page.evaluate(
            """(role) => {
                const sel = document.querySelector(
                    '[id="formPublica:tipo"], select[name*="tipo"]'
                );
                if (sel) {
                    for (const option of sel.options) {
                        if (option.text.trim().toUpperCase() === role) {
                            sel.value = option.value;
                        }
                    }
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            str(args.role).upper(),
        )
        page.fill('[id="formPublica:nomIntervParte"]', str(args.parte).upper())

    return _run_search(args, fill, '[id="formPublica:buscarPorParteButton"]')


def handle_rh(args: argparse.Namespace) -> dict[str, Any]:
    def fill(page: Any) -> None:
        _click_tab(page, "Reparación Histórica")
        page.fill('[id="formPublica:nombreInterveniente"]', str(args.nombre).upper())
        page.fill('[id="formPublica:apellidoInterveniente"]', str(args.apellido).upper())

    return _run_search(args, fill, '[id="formPublica:buscarRHButton"]')


def _run_search(
    args: argparse.Namespace,
    fill_fn: Callable[[Any], None],
    submit_selector: str,
) -> dict[str, Any]:
    operation = _operation_from_args(args)
    query = _query_from_args(operation, args)
    retries = max(0, int(getattr(args, "retries", 3)))
    pace = max(0, int(getattr(args, "pace", 0) or 0))
    last_error: str | None = None

    try:
        with BotBrowser(hidden=not bool(getattr(args, "show", False))) as browser:
            page = browser.page
            if page is None:
                return _search_failure(
                    operation,
                    query,
                    attempts=0,
                    last_error="browser page was not created",
                )

            for attempt in range(retries):
                attempt_number = attempt + 1
                try:
                    _open_and_wait(page)
                    page.wait_for_timeout(pace)
                    fill_fn(page)
                    page.wait_for_timeout(pace)
                    if not _solve_pjn_captcha(
                        page,
                        attempts=DEFAULT_CAPTCHA_ATTEMPTS,
                        pace=pace,
                    ):
                        last_error = "captcha failed"
                        continue

                    if not _submit_search(page, submit_selector):
                        last_error = f"submit control not found: {submit_selector}"
                        continue

                    page.wait_for_timeout(5000 + pace)
                    result = _parse_results(page)
                    if (
                        result.get("row_count")
                        or result.get("no_results")
                        or result.get("links")
                    ):
                        page.wait_for_timeout(pace + 2000)
                        return _search_success(
                            operation=operation,
                            query=query,
                            attempt=attempt_number,
                            result=result,
                        )

                    last_error = "search did not produce recognizable results"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    try:
                        page.wait_for_timeout(1000 + pace)
                    except Exception:
                        pass
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"

    return _search_failure(
        operation,
        query,
        attempts=retries,
        last_error=last_error,
    )


def _solve_pjn_captcha(
    page: Any,
    attempts: int = DEFAULT_CAPTCHA_ATTEMPTS,
    pace: int = 0,
) -> bool:
    """Solve PJN's image challenge and wait for the parent captcha token."""

    frame = _captcha_frame(page)
    if frame is None:
        return False

    try:
        frame.locator(".terminos-button").click(timeout=5000)
        page.wait_for_timeout(1200 + pace)
    except Exception:
        pass

    for attempt in range(max(0, attempts)):
        if attempt > 0:
            try:
                frame.locator(".challenge-button-refresh").click(timeout=4000)
                page.wait_for_timeout(700 + pace)
            except Exception:
                pass

        src = _captcha_image_src(frame, page)
        if not src:
            continue

        page.wait_for_timeout(300 + pace)
        try:
            answer = solve_image(src).strip()
        except Exception:
            continue
        if not answer:
            continue

        try:
            answer_input = frame.locator(".text-challenge-input").first
            answer_input.click()
            answer_input.press("Control+a")
            answer_input.press("Delete")
            answer_input.type(answer, delay=160 if pace else 110)
            page.wait_for_timeout(pace)
            frame.locator(".accept-challenge-button").click(timeout=4000)
        except Exception:
            continue

        if _wait_for_captcha_token(page, pace=pace):
            return True

    return False


def _open_and_wait(page: Any) -> None:
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#formPublica", timeout=20000)
    page.wait_for_timeout(2500)


def _click_tab(page: Any, label: str) -> None:
    page.evaluate(
        """(label) => {
            const el = [...document.querySelectorAll('a,span,li,div')]
                .find(e => e.innerText.trim() === label);
            if (el) el.click();
        }""",
        label,
    )
    page.wait_for_timeout(2500)


def parse_results_html(html: str) -> dict[str, Any]:
    root = parse_html(html)
    rows: list[list[str]] = []
    for row in root.iter("tr"):
        cells = [cell.text() for cell in row.iter("td")]
        values = [cell for cell in cells if cell]
        if values:
            rows.append(values)

    links = [
        href
        for href in (
            link.get("href")
            for link in root.iter("a")
        )
        if href and re.search(r"expediente|consultaExp|verExp|detalle", href, re.IGNORECASE)
    ][:30]

    body = root.find("body")
    text = (body.text() if body is not None else root.text()) or ""
    no_results = bool(
        re.search(r"no se (encontr|hallar)|sin resultados|no existen", text, re.IGNORECASE)
    )
    return {
        "row_count": len(rows),
        "rows": rows[:50],
        "links": links,
        "no_results": no_results,
        "text": text[:1500],
    }


def _parse_results(page: Any) -> dict[str, Any]:
    try:
        html = page.content()
    except Exception:
        return _parse_results_js(page)

    if isinstance(html, str) and html.strip():
        return parse_results_html(html)
    return _parse_results_js(page)


def _parse_results_js(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const rows = [...document.querySelectorAll('table tr')].map(tr =>
                [...tr.querySelectorAll('td')]
                    .map(td => td.innerText.trim())
                    .filter(Boolean)
            ).filter(r => r.length);
            const links = [...document.querySelectorAll('a[href]')]
                .map(a => a.getAttribute('href'))
                .filter(h => /expediente|consultaExp|verExp|detalle/i.test(h))
                .slice(0, 30);
            const text = document.body.innerText;
            const noResults = /no se (encontr|hallar)|sin resultados|no existen/i.test(text);
            return {
                row_count: rows.length,
                rows: rows.slice(0, 50),
                links,
                no_results: noResults,
                text: text.slice(0, 1500),
            };
        }"""
    )


def _submit_search(page: Any, submit_selector: str) -> bool:
    return bool(
        page.evaluate(
            """(sel) => {
                const button = document.querySelector(sel)
                    || [...document.querySelectorAll('button,input[type=submit],a')]
                        .find(e => /consultar|buscar/i.test(e.innerText || e.value || ''));
                if (!button) {
                    return false;
                }
                button.click();
                return true;
            }""",
            submit_selector,
        )
    )


def _search_success(
    *,
    operation: str,
    query: dict[str, Any],
    attempt: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    links = result.get("links") if isinstance(result.get("links"), list) else []
    row_count = result.get("row_count")
    if not isinstance(row_count, int):
        row_count = len(rows)

    raw = {
        "attempt": attempt,
        "row_count": row_count,
        "no_results": bool(result.get("no_results")),
    }
    if isinstance(result.get("text"), str):
        raw["text"] = result["text"]

    return {
        "ok": True,
        "source": SOURCE_ID,
        "operation": operation,
        "op": operation,
        "query": query,
        "attempt": attempt,
        "row_count": row_count,
        "rows": rows,
        "links": links,
        "no_results": bool(result.get("no_results")),
        "provenance": _provenance(raw=raw).to_dict(),
    }


def _search_failure(
    operation: str,
    query: dict[str, Any],
    *,
    attempts: int,
    last_error: str | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"attempts": attempts}
    if last_error:
        details["last_error"] = last_error
    return {
        "ok": False,
        "source": SOURCE_ID,
        "operation": operation,
        "op": operation,
        "query": query,
        "attempt": attempts,
        "error": LegalError(
            code="source_unavailable",
            message="captcha or search failed after retries",
            retryable=True,
            details=details,
        ).to_dict(),
        "provenance": _provenance(raw=details).to_dict(),
    }


def _operation_from_args(args: argparse.Namespace) -> str:
    operation = getattr(args, "operation", None)
    if isinstance(operation, str) and operation:
        return operation
    if hasattr(args, "numero") and hasattr(args, "anio"):
        return "expediente"
    if hasattr(args, "parte"):
        return "parte"
    if hasattr(args, "apellido"):
        return "rh"
    return "search"


def _query_from_args(operation: str, args: argparse.Namespace) -> dict[str, Any]:
    if operation == "expediente":
        return {
            "camara": _require_arg(args, "camara", operation=operation),
            "numero": _require_arg(args, "numero", operation=operation),
            "anio": _require_arg(args, "anio", operation=operation),
        }
    if operation == "parte":
        return {
            "camara": _require_arg(args, "camara", operation=operation),
            "role": _require_arg(args, "role", operation=operation).upper(),
            "parte": _require_arg(args, "parte", operation=operation).upper(),
        }
    if operation == "rh":
        return {
            "nombre": _require_arg(args, "nombre", operation=operation).upper(),
            "apellido": _require_arg(args, "apellido", operation=operation).upper(),
        }
    return {}


def _require_arg(args: argparse.Namespace, name: str, *, operation: str) -> str:
    value = getattr(args, name, None)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise LegalCliError(
            code="usage_error",
            message=f"{operation} requires --{name}",
            details={"source": SOURCE_ID, "operation": operation},
        )
    return text


def _add_browser_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=3,
        help="captcha/search attempts",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        default=False,
        help="show the BotBrowser window instead of running hidden",
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return parsed


def _captcha_frame(page: Any) -> Any | None:
    frame = page.frame(name=CAPTCHA_FRAME_NAME)
    if frame is not None:
        return frame

    try:
        page.wait_for_selector(CAPTCHA_FRAME_SELECTOR, timeout=15000)
    except Exception:
        return None
    return page.frame(name=CAPTCHA_FRAME_NAME)


def _captcha_image_src(frame: Any, page: Any) -> str | None:
    for _ in range(40):
        src = frame.evaluate(
            """() => {
                const img = document.querySelector('.text-challenge-image img');
                return img && img.src && img.src.startsWith('data:') ? img.src : null;
            }"""
        )
        if src:
            return src
        page.wait_for_timeout(200)
    return None


def _wait_for_captcha_token(page: Any, *, pace: int = 0) -> bool:
    for _ in range(48):
        token = page.evaluate(
            """() => {
                const input = document.querySelector('#captcha-response');
                return input && input.value ? input.value : null;
            }"""
        )
        if token:
            page.wait_for_timeout(pace)
            return True
        page.wait_for_timeout(250)
    return False


def _provenance(*, raw: dict[str, Any] | None = None) -> Provenance:
    return Provenance.now(
        source_urls=[HOME_URL],
        fetched_urls=[],
        source_map=SOURCE_MAP,
        raw=raw or {},
    )


def build_adapter() -> SourceAdapter:
    adapter = SourceAdapter(SOURCE_BY_ID[SOURCE_ID])
    adapter.register_operation(
        "expediente",
        handle_expediente,
        help="search PJN expedientes by chamber, number, and year",
        add_arguments=add_expediente_arguments,
    )
    adapter.register_operation(
        "parte",
        handle_parte,
        help="search PJN expedientes by party",
        add_arguments=add_parte_arguments,
    )
    adapter.register_operation(
        "rh",
        handle_rh,
        help="search PJN reparación histórica expedientes",
        add_arguments=add_rh_arguments,
    )
    adapter.register_operation(
        "camaras",
        handle_camaras,
        help="list PJN expediente jurisdictions",
    )
    return adapter


register_adapter(build_adapter(), replace=True)

__all__ = [
    "BotBrowser",
    "CAMARAS",
    "HOME_URL",
    "SOURCE_ID",
    "SOURCE_MAP",
    "add_expediente_arguments",
    "add_parte_arguments",
    "add_rh_arguments",
    "build_adapter",
    "handle_camaras",
    "handle_expediente",
    "handle_parte",
    "handle_rh",
    "parse_results_html",
]
