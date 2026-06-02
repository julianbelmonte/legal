"""Global cross-source search tool for the Argentina legal research MCP surface.

This tool fans a single query out across the selected *direct* Argentine legal
sources (official bulletins, normative and jurisprudence databases, ...) and
aggregates the per-source results into one normalized envelope. It reuses the
shared global-search core (:func:`legal.global_search.run_global_search`) -- the
exact same function behind the API ``POST /v1/search`` route and the CLI
``search`` command -- so the MCP surface produces a byte-identical envelope and
adds no source-access logic of its own.

Source selection mirrors the API/CLI contract: pass ``all_direct=True`` to fan
out across every direct source, or a non-empty ``sources`` list to target
specific ones. Invalid selections (unknown or non-direct sources, or neither
selector) are validated inside the shared core, which raises a
``LegalCliError`` that surfaces as the normalized ``usage_error`` envelope.
"""

from __future__ import annotations

from typing import Any

import legal.global_search

from mcp_server.serialization import to_jsonable


def legal_search(
    text: str,
    sources: list[str] | None = None,
    all_direct: bool = False,
    limit_per_source: int = 5,
    raw: bool = False,
) -> dict[str, Any]:
    """Search across the wired Argentina legal sources and aggregate results.

    Fans ``text`` out across the selected direct sources and returns the
    aggregated normalized envelope (tagged ``items``, per-source facets,
    pagination, provenance, and warnings). Select sources with either
    ``all_direct=True`` (every direct source) or a non-empty ``sources`` list;
    ``limit_per_source`` caps the hits requested from each source. Set ``raw`` to
    include each source's raw provider payload. Mirrors the API
    ``POST /v1/search`` route and the CLI ``search`` command 1:1 -- it delegates
    to ``legal.global_search.run_global_search`` and adds no source-access
    logic. Invalid selections yield the normalized ``usage_error`` envelope; a
    total fan-out failure yields a ``source_unavailable`` error envelope.
    """
    result = legal.global_search.run_global_search(
        text=text,
        sources=sources,
        all_direct=bool(all_direct),
        limit_per_source=limit_per_source,
        raw=bool(raw),
    )
    return to_jsonable(result)
