"""Generic, source-agnostic run tool for the Argentina legal research MCP surface.

This tool mirrors the API's uniform ``POST /v1/sources/{source_id}/{operation}``
route: it runs *any* registered source/operation pair from a plain parameter
dictionary by delegating to :func:`legal.dispatch.run_operation`, returning the
same normalized envelope. It adds no source-access logic of its own.

Unlike the API route, the MCP surface must **not** expose remote PDF downloads,
filesystem save paths, or raw binary retrieval -- an MCP client runs on a remote
host and has no access to the server's filesystem, and raw bytes are not
JSON-serializable through the MCP transport. :func:`reject_unsafe_mcp_params`
encodes that policy: it rejects any parameter that would request a save-to-disk
path, a downloadable artifact, or raw PDF/binary output, while leaving ordinary
search/get parameters (``text``, ``id``, ...) untouched.
"""

from __future__ import annotations

from typing import Any, Mapping

import legal.dispatch

from mcp_server.serialization import error_envelope, to_jsonable

__all__ = [
    "UnsafeMcpParamsError",
    "reject_unsafe_mcp_params",
    "legal_run_operation",
]


# Substrings that, when present in a parameter key, imply a filesystem save path,
# a downloadable artifact, or raw binary output that the MCP surface must not
# expose. Matched case-insensitively against the dash/underscore-normalized key.
_UNSAFE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "save_pdf",
    "save_path",
    "output_path",
    "out_path",
    "outfile",
    "download_path",
    "save_bytes",
    "save_html",
    "save_to",
)

# Exact normalized keys that request raw/binary download output. These are
# matched exactly (not as substrings) so a normal ``{"text": ...}`` is safe.
_UNSAFE_EXACT_KEYS: frozenset[str] = frozenset(
    {
        "save_pdf",
        "savepdf",
        "pdf",
        "raw_pdf",
        "raw_bytes",
        "bytes",
        "download",
        "outfile",
        "outpath",
    }
)


class UnsafeMcpParamsError(ValueError):
    """Raised when MCP request params would expose file writes or raw downloads.

    Carries the offending parameter key so callers can render a precise
    normalized usage-error envelope.
    """

    def __init__(self, key: str, *, source_id: str, operation: str) -> None:
        self.key = key
        self.source_id = source_id
        self.operation = operation
        super().__init__(
            f"parameter {key!r} is not allowed over MCP: it would request a "
            f"filesystem save path, a downloadable artifact, or raw binary "
            f"output, which the MCP surface does not expose"
        )


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def reject_unsafe_mcp_params(
    source_id: str,
    operation: str,
    params: Mapping[str, Any] | None,
) -> None:
    """Raise :class:`UnsafeMcpParamsError` if ``params`` request unsafe output.

    Returns ``None`` when every parameter is safe. A parameter is unsafe when its
    key implies a filesystem save path, a downloadable artifact, or raw
    PDF/binary output -- e.g. ``save_pdf``/``save-pdf``, ``output_path``,
    ``out_path``, ``outfile``, ``save_path``, ``download_path``, or an explicit
    ``pdf``/raw-bytes request. Matching is conservative-but-precise: keys are
    normalized (dashes to underscores, lowercased) and tested against a set of
    save/download substrings plus a set of exact raw-output keys, so an ordinary
    ``{"text": "x"}`` for ``("saij", "search")`` is **not** rejected.
    """
    if not params:
        return None
    for raw_key in params:
        normalized = _normalize_key(raw_key)
        if normalized in _UNSAFE_EXACT_KEYS:
            raise UnsafeMcpParamsError(
                str(raw_key), source_id=source_id, operation=operation
            )
        for marker in _UNSAFE_KEY_SUBSTRINGS:
            if marker in normalized:
                raise UnsafeMcpParamsError(
                    str(raw_key), source_id=source_id, operation=operation
                )
    return None


def legal_run_operation(
    source_id: str,
    operation: str,
    params: Mapping[str, Any] | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """Run any registered Argentina legal source/operation from a params dict.

    Mirrors the API uniform route ``POST /v1/sources/{source_id}/{operation}``
    1:1 -- it delegates to :func:`legal.dispatch.run_operation` and adds no
    source-access logic. Before dispatching, MCP-inappropriate parameters
    (filesystem save paths such as ``save_pdf``/``save-pdf``, raw PDF/binary
    requests, or other downloadable-artifact keys) are rejected via
    :func:`reject_unsafe_mcp_params`; a rejected request yields the normalized
    ``usage_error`` envelope instead of raising to the caller. Unknown
    source/operation pairs and bad params raise the pipeline's ``LegalCliError``
    as usual; the returned value is the serialized normalized envelope.
    """
    try:
        reject_unsafe_mcp_params(source_id, operation, params)
    except UnsafeMcpParamsError as exc:
        return error_envelope(
            source=source_id,
            operation=operation,
            message=str(exc),
            code="usage_error",
        )

    result = legal.dispatch.run_operation(
        source_id,
        operation,
        params or {},
        raw=bool(raw),
    )
    return to_jsonable(result)
