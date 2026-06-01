"""Registry of legal sources included in the portable CLI."""

from __future__ import annotations

from legal.models import SourceInfo, UnsupportedOperation


CAPTCHA_SOLVER_CAPABILITY = "captcha_solver"
UNSUPPORTED_CAPTCHA_ERROR = "unsupported_captcha"
# Sources whose `search` needs mandatory filters (organism, date window, ...) and
# therefore cannot satisfy a bare keyword `--all-direct` fan-out query.
REQUIRES_SEARCH_FILTERS_CAPABILITY = "requires_search_filters"


def _captcha_protected(operation: str, reason: str) -> UnsupportedOperation:
    return UnsupportedOperation(
        operation=operation,
        error_code=UNSUPPORTED_CAPTCHA_ERROR,
        capability_required=CAPTCHA_SOLVER_CAPABILITY,
        reason=reason,
    )


SOURCES: tuple[SourceInfo, ...] = (
    SourceInfo(
        id="aaip",
        name="AAIP",
        operations=["sync", "search", "get"],
        source_map="legal/docs/aaip_disposiciones.md",
    ),
    SourceInfo(
        id="bcra",
        name="BCRA",
        operations=["filters", "search", "download"],
        source_map="legal/docs/bcra_normativa.md",
    ),
    SourceInfo(
        id="bo-nacional",
        name="Boletin Oficial Nacional",
        operations=["filters", "search", "get", "next"],
        source_map="legal/docs/boletin_oficial_nacional.md",
    ),
    SourceInfo(
        id="bo-pba",
        name="Boletin Oficial PBA",
        operations=["search", "pages", "section", "get", "next", "pdf"],
        source_map="legal/docs/boletin_oficial_pba.md",
    ),
    SourceInfo(
        id="cnacaf",
        name="CNACAF",
        operations=["filters", "search", "pdf", "pjn-search"],
        source_map="legal/docs/cnacaf_jurisprudencia.md",
    ),
    SourceInfo(
        id="csjn",
        name="CSJN",
        operations=["fallos", "sumarios", "documento", "download"],
        source_map="legal/docs/csjn_jurisprudencia.md",
        browser_required=True,
    ),
    SourceInfo(
        id="dppj",
        name="DPPJ",
        operations=["list", "search", "get", "sync"],
        source_map="legal/docs/dppj_resoluciones.md",
    ),
    SourceInfo(
        id="igj",
        name="IGJ",
        operations=["list", "search", "get-infoleg", "scrape-official-page"],
        source_map="legal/docs/igj_resoluciones.md",
    ),
    SourceInfo(
        id="infoleg",
        name="Infoleg",
        operations=["search", "get", "links", "next"],
        source_map="legal/docs/infoleg_normas_nacionales.md",
    ),
    SourceInfo(
        id="juba",
        name="JUBA",
        operations=["search", "buckets", "get", "next"],
        source_map="legal/docs/juba_scba.md",
    ),
    SourceInfo(
        id="jusbaires",
        name="Jusbaires",
        operations=["search", "descriptors", "fallo", "sumario"],
        source_map="legal/docs/jusbaires_jurisprudencia.md",
    ),
    SourceInfo(
        id="normas-pba",
        name="Normas PBA",
        operations=["search", "get", "related", "download"],
        source_map="legal/docs/normas_pba.md",
    ),
    SourceInfo(
        id="pjn-expedientes",
        name="PJN Expedientes",
        operations=["expediente", "parte", "rh", "camaras"],
        source_map="legal/docs/pjn_expedientes.md",
        browser_required=True,
    ),
    SourceInfo(
        id="pjn-juris",
        name="PJN Jurisprudencia",
        operations=["facets", "search", "download"],
        source_map="legal/docs/pjn_jurisprudencia.md",
    ),
    SourceInfo(
        id="ptn",
        name="PTN",
        operations=["search", "download"],
        source_map="legal/docs/ptn_dictamenes.md",
    ),
    SourceInfo(
        id="saij",
        name="SAIJ",
        operations=["facets", "search", "get", "download"],
        source_map="legal/docs/saij_jurisprudencia.md",
    ),
    SourceInfo(
        id="sentencias-scba",
        name="Sentencias SCBA",
        operations=["organisms", "search", "get", "pdf", "anonymize"],
        source_map="legal/docs/sentencias_scba.md",
        capabilities=[REQUIRES_SEARCH_FILTERS_CAPABILITY],
    ),
    SourceInfo(
        id="tfn",
        name="TFN",
        operations=["filters", "search", "latest", "summary", "pdf"],
        source_map="legal/docs/tfn_jurisprudencia.md",
    ),
)

SOURCE_IDS: tuple[str, ...] = tuple(source.id for source in SOURCES)
SOURCE_BY_ID: dict[str, SourceInfo] = {source.id: source for source in SOURCES}


def list_sources() -> list[dict]:
    # Reflect the *actually wired* surface: operations declared in the registry
    # but lacking a concrete adapter handler are reported under
    # `unsupported_operations` instead of being advertised as working, so callers
    # don't burn a call on an operation that only returns `unsupported_operation`.
    from legal.sources import get_adapter

    payloads = []
    for source in SOURCES:
        payload = source.to_dict()
        adapter = get_adapter(source.id)
        already = {entry["operation"] for entry in payload.get("unsupported_operations", [])}
        missing = [
            op
            for op in source.operations
            if op not in already and not (adapter and adapter.get_operation(op))
        ]
        if missing:
            payload["operations"] = [op for op in payload["operations"] if op not in missing]
            payload["unsupported_operations"] = payload.get("unsupported_operations", []) + [
                UnsupportedOperation(
                    operation=op,
                    error_code="unsupported_operation",
                    reason=f"{source.id} {op} does not have an adapter implementation yet",
                ).to_dict()
                for op in missing
            ]
        payloads.append(payload)
    return payloads


def get_source(source_id: str) -> SourceInfo | None:
    return SOURCE_BY_ID.get(source_id)
