"""Shared global cross-source search core.

This module holds the global-search fan-out aggregation that both the CLI
(``cli.cmd_global_search``) and the FastAPI ``/v1/search`` endpoint use, so the
two consumers produce a byte-identical normalized envelope.

It deliberately reuses the private helpers in :mod:`legal.cli`
(``_select_global_search_sources``, ``_source_operation``,
``_build_global_source_search_args``, ``_result_payload``, ``_tag_global_item``,
``_extend_unique``, ``_mapping_or_empty``) so the aggregation never diverges
from the CLI's behavior. ``cli`` imports ``run_global_search`` lazily (inside
``cmd_global_search``) to avoid an import cycle.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from legal.cli import (
    _build_global_source_search_args,
    _extend_unique,
    _global_search_op_name,
    _mapping_or_empty,
    _result_payload,
    _select_global_search_sources,
    _source_operation,
    _tag_global_item,
)
from legal.errors import LegalCliError
from legal.models import LegalError, LegalResponse, PageInfo, Provenance

__all__ = ["run_global_search"]


def run_global_search(
    *,
    text: str,
    sources: list[str] | None,
    all_direct: bool,
    limit_per_source: int,
    raw: bool = False,
) -> LegalResponse:
    """Fan out a search across the selected direct sources and aggregate.

    ``sources``/``all_direct`` are validated by the shared selector
    (``_select_global_search_sources``), which raises ``LegalCliError``
    (``usage_error``) for unknown or non-direct sources so every consumer gets
    the same validation. Returns the unmodified normalized envelope
    (a ``LegalResponse``); on a total failure it returns an error response.
    """
    selection_args = argparse.Namespace(
        all_direct=bool(all_direct),
        global_sources=list(sources or []),
    )
    source_ids = _select_global_search_sources(selection_args)
    query = {
        "text": text,
        "sources": source_ids,
        "all_direct": bool(all_direct),
        "limit_per_source": limit_per_source,
        "raw": bool(raw),
    }
    items: list[Any] = []
    source_results: dict[str, dict[str, Any]] = {}
    source_errors: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    fetched_urls: list[str] = []
    source_urls: list[str] = []
    has_more = False
    total = 0
    total_known = True
    success_count = 0

    for source_id in source_ids:
        op_name = _global_search_op_name(source_id, text)
        if op_name is None:
            # Source has no search-like operation (e.g. download-only, or a
            # browser source with no mapped search op). Skip gracefully and
            # report it instead of aborting the whole fan-out.
            skip_error = LegalError(
                code="unsupported_operation",
                message=f"{source_id} does not support search",
                retryable=False,
                details={"source": source_id},
            )
            source_errors[source_id] = skip_error.to_dict()
            source_results[source_id] = {
                "ok": False,
                "error": skip_error.to_dict(),
                "warnings": [],
            }
            warnings.append(f"{source_id} skipped: no search operation")
            continue
        try:
            operation = _source_operation(source_id, op_name)
            source_args = _build_global_source_search_args(
                source_id=source_id,
                operation=operation,
                text=text,
                limit=limit_per_source,
                raw=bool(raw),
            )
            result = operation.handler(source_args)
            payload = _result_payload(result)
        except LegalCliError as error:
            error_payload = error.to_error().to_dict()
            source_errors[source_id] = error_payload
            source_results[source_id] = {
                "ok": False,
                "error": error_payload,
                "warnings": [],
            }
            warnings.append(f"{source_id} search failed: {error.code}")
            if error.provenance is not None:
                _extend_unique(source_urls, error.provenance.source_urls)
                _extend_unique(fetched_urls, error.provenance.fetched_urls)
            continue
        except Exception as exc:  # pragma: no cover - defensive contract guard
            error = LegalError(
                code="parse_error",
                message=f"{source_id} search failed unexpectedly",
                retryable=False,
                details={"source": source_id, "exception_type": type(exc).__name__},
            )
            error_payload = error.to_dict()
            source_errors[source_id] = error_payload
            source_results[source_id] = {
                "ok": False,
                "error": error_payload,
                "warnings": [],
            }
            warnings.append(f"{source_id} search failed: parse_error")
            continue

        provenance = payload.get("provenance")
        if isinstance(provenance, Mapping):
            _extend_unique(source_urls, provenance.get("source_urls", []))
            _extend_unique(fetched_urls, provenance.get("fetched_urls", []))

        page = payload.get("page")
        page_payload = dict(page) if isinstance(page, Mapping) else {}
        source_warnings = [str(warning) for warning in payload.get("warnings", [])]
        if not payload.get("ok", True):
            error_payload = _mapping_or_empty(payload.get("error"))
            source_errors[source_id] = error_payload
            source_results[source_id] = {
                "ok": False,
                "error": error_payload,
                "page": page_payload,
                "warnings": source_warnings,
            }
            warnings.append(
                f"{source_id} search failed: {error_payload.get('code', 'source_error')}"
            )
            continue

        source_items = list(payload.get("items") or [])
        tagged_items = [_tag_global_item(item, source_id) for item in source_items]
        items.extend(tagged_items)
        success_count += 1
        has_more = has_more or bool(page_payload.get("has_more", False))
        page_total = page_payload.get("total")
        if isinstance(page_total, int):
            total += page_total
        else:
            total_known = False
        source_results[source_id] = {
            "ok": True,
            "item_count": len(source_items),
            "page": page_payload,
            "query": _mapping_or_empty(payload.get("query")),
            "warnings": source_warnings,
        }
        warnings.extend(f"{source_id}: {warning}" for warning in source_warnings)

    provenance = Provenance(
        source_urls=source_urls,
        fetched_urls=fetched_urls,
        source_map="legal/registry.py",
        raw={"sources": source_results},
    )
    if success_count == 0:
        return LegalResponse.error_response(
            source="legal",
            operation="search",
            query=query,
            error=LegalError(
                code="source_unavailable",
                message="all selected source searches failed",
                retryable=any(
                    error.get("retryable") is True for error in source_errors.values()
                ),
                details={"sources": source_errors},
            ),
            provenance=provenance,
            warnings=warnings,
        )

    return LegalResponse.search(
        source="legal",
        operation="search",
        query=query,
        items=items,
        page=PageInfo(
            limit=limit_per_source * len(source_ids),
            total=total if total_known else None,
            has_more=has_more,
        ),
        provenance=provenance,
        warnings=warnings,
        facets={
            "sources": source_results,
            "source_errors": source_errors,
            "pagination": {
                source_id: result.get("page", {})
                for source_id, result in source_results.items()
                if result.get("page")
            },
        },
    )
