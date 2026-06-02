"""Discovery tools for the Argentina legal research MCP surface.

These tools let an agent discover *which* Argentine legal sources are wired
(courts, official bulletins, normative databases, ...), what operations each
source supports, and the normalized response envelope every source returns. They
mirror the API discovery routes (``GET /v1/sources``,
``GET /v1/sources/{source_id}``, ``GET /v1/schema``) 1:1 and reuse the same
pipeline seams (:mod:`legal.registry` for source/provenance discovery and
:mod:`legal.schema` for the response schema), adding no source-access logic.

Every source dict carries provenance metadata -- the human-readable source name,
its ``source_map`` doc reference, advertised vs. unsupported operations, and
whether a browser/captcha path is required -- so an agent can pick the right
Argentina legal source and operation before invoking it.
"""

from __future__ import annotations

from typing import Any

import legal.registry
import legal.schema
from legal.errors import not_found

from mcp_server.serialization import to_jsonable


def legal_sources() -> dict[str, Any]:
    """List the wired Argentina legal research sources and their operations.

    Returns the registry of Argentine legal sources (courts, official bulletins,
    normative and jurisprudence databases) as ``{"items": [...]}``. Each item is
    a normalized source dict that carries provenance metadata: the source id and
    name, its ``source_map`` documentation reference, the operations it
    advertises, any unsupported operations, and whether it requires the
    browser/captcha path. Mirrors the API ``GET /v1/sources`` discovery route.
    """
    return to_jsonable({"items": legal.registry.list_sources()})


def legal_source(source_id: str) -> dict[str, Any]:
    """Describe one Argentina legal research source by id.

    Returns the normalized source dict for ``source_id`` -- its name, supported
    and unsupported operations, ``source_map`` provenance reference, and browser
    requirement -- so an agent can confirm an Argentine legal source's surface
    before invoking it. Unknown ids yield the pipeline's normalized
    ``ok: false`` not-found envelope rather than raising. Mirrors the API
    ``GET /v1/sources/{source_id}`` discovery route.
    """
    source = legal.registry.get_source(source_id)
    if source is None:
        envelope = (
            not_found(
                f"unknown source: {source_id}",
                details={"source_id": source_id},
            )
            .to_response(source=source_id, operation="get_source")
            .to_dict()
        )
        return to_jsonable(envelope)
    return to_jsonable(source.to_dict())


def legal_schema() -> dict[str, Any]:
    """Return the normalized response envelope schema for legal sources.

    Returns the JSON Schema describing the uniform response envelope every
    Argentina legal research source emits (success items/document/facets,
    pagination, provenance, and error shapes). Use it to understand result
    structure and provenance fields before parsing tool output. Mirrors the API
    ``GET /v1/schema`` discovery route.
    """
    return to_jsonable(legal.schema.LEGAL_RESPONSE_SCHEMA)
