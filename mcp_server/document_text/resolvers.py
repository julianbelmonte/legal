"""Source-ID → document-text-strategy registry for the MCP server.

Each legal source retrieves a single document through a different operation and
shapes its text/metadata/url differently. The MCP ``legal_get_document_text``
tool needs one uniform entry point, so this module declares — per source — the
**existing** pipeline operation that the tool will dispatch through
``legal.dispatch.run_operation`` and how to read text/metadata/url/provenance
out of the resulting normalized envelope.

Design only. This module wires the *mapping*; it performs no source access and
reimplements nothing. The actual fetch + extraction + TTL-cache + cursor paging
land in later steps (10-13), which consume the strategies declared here.

Public surface:

- :func:`get_document_text_resolver` — return a :class:`DocumentTextResolver`
  for a supported source id, or ``None`` for an unsupported one.
- :func:`document_text_error` — build the normalized error envelope an
  unsupported source must return.
- :func:`supported_document_text_sources` — the supported source ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from legal.dispatch import resolve_operation, run_operation
from legal.errors import usage_error
from legal.models import JsonDict, LegalResponse
from legal.registry import SOURCE_BY_ID

__all__ = [
    "DocumentTextResolver",
    "DocumentTextStrategy",
    "TextMode",
    "document_text_error",
    "get_document_text_resolver",
    "supported_document_text_sources",
]


class TextMode(str, Enum):
    """How a source's document operation surfaces extracted text.

    - ``DIRECT`` — the document body is returned directly by the operation with
      no opt-in flag (e.g. ``sentencias-scba get`` returns HTML detail text,
      ``infoleg get`` returns norm text for its non-metadata text modes).
    - ``TEXT_FLAG`` — the operation only includes extracted PDF text when an
      opt-in flag is set (``legal.enrichment.add_text_arguments`` registers
      ``--text`` → ``want_text``). The MCP layer always requests text-only and
      never ``save_pdf``/raw bytes.
    - ``PDF_TEXT`` — the operation is PDF-backed and the pipeline extracts text
      via ``legal.pdf.extract_text``; for the priority sources this is reached
      through the ``TEXT_FLAG`` opt-in, but some operations (e.g.
      ``pjn-juris download``) need explicit text-extraction wiring added in a
      later step rather than a ready ``want_text`` flag.
    """

    DIRECT = "direct"
    TEXT_FLAG = "text_flag"
    PDF_TEXT = "pdf_text"


@dataclass(frozen=True)
class DocumentTextStrategy:
    """Declarative mapping from a source id to its document-text operation.

    This records *which* existing operation the MCP document-text tool will
    invoke and *how* to read text out of the envelope it returns. It is data,
    not behavior: later steps consume these fields to dispatch and normalize.

    Attributes:
        source_id: Registry source id (must exist in ``legal.registry``).
        operation: The existing operation to dispatch via ``run_operation``.
        id_params: Accepted parameter name(s) carrying the document identifier,
            in preference order. The MCP tool maps its ``document_id`` onto the
            first of these the operation's parser accepts.
        text_mode: How the operation surfaces extracted text (see
            :class:`TextMode`).
        want_text_param: The opt-in flag name to set truthy for ``TEXT_FLAG``
            operations (``want_text`` via ``--text``); ``None`` for ``DIRECT``
            operations.
        extra_params: Static params always passed to the operation (e.g.
            infoleg's ``text`` mode selecting original/updated text instead of
            metadata).
        notes: Human-readable note about the source's text/url shape.
    """

    source_id: str
    operation: str
    id_params: tuple[str, ...]
    text_mode: TextMode
    want_text_param: str | None = None
    extra_params: JsonDict = field(default_factory=dict)
    notes: str | None = None

    def build_params(self, document_id: str, *, overrides: Mapping[str, Any] | None = None) -> JsonDict:
        """Build the ``run_operation`` params dict for this document fetch.

        Sets the primary id param, the text opt-in (when applicable), and any
        static ``extra_params``. ``overrides`` lets a caller add operation
        params (date windows, register, ...) without re-deriving the mapping.
        The MCP layer must never inject ``save_pdf``/``save-pdf``/``out`` or any
        raw-byte/output-path param; this builder emits none of those.
        """
        params: JsonDict = dict(self.extra_params)
        params[self.id_params[0]] = document_id
        if self.want_text_param is not None:
            params[self.want_text_param] = True
        if overrides:
            params.update(dict(overrides))
        return params


@dataclass(frozen=True)
class DocumentTextResolver:
    """A bound resolver: a strategy plus the dispatch seam to execute it.

    Kept intentionally thin for this design step. ``fetch`` is the attachment
    point the later extraction/cache/paging steps build on; it dispatches the
    mapped operation through the existing ``legal.dispatch.run_operation`` seam
    and returns the unchanged normalized envelope. No text slicing, caching, or
    cursoring happens here yet.
    """

    strategy: DocumentTextStrategy

    @property
    def source_id(self) -> str:
        return self.strategy.source_id

    @property
    def operation(self) -> str:
        return self.strategy.operation

    def with_strategy(self, **changes: Any) -> "DocumentTextResolver":
        """Return a resolver whose strategy has the given fields replaced."""
        return DocumentTextResolver(strategy=replace(self.strategy, **changes))

    def fetch(
        self,
        document_id: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        raw: bool = False,
    ) -> LegalResponse | Mapping[str, Any]:
        """Dispatch the mapped operation and return the normalized envelope.

        Reuses ``legal.dispatch.run_operation`` exactly like the API/CLI do; it
        adds no source-access logic. Text extraction, TTL caching, and cursor
        paging are layered on by later steps that call this.
        """
        params = self.strategy.build_params(document_id, overrides=overrides)
        return run_operation(self.source_id, self.operation, params, raw=raw)


# Priority sources first (saij, infoleg, csjn, ptn, sentencias-scba, pjn-juris),
# then the remaining text-bearing sources. Every operation referenced here is an
# existing, registry-declared operation; nothing new is invented.
_STRATEGIES: tuple[DocumentTextStrategy, ...] = (
    DocumentTextStrategy(
        source_id="saij",
        operation="download",
        id_params=("guid", "id"),
        text_mode=TextMode.TEXT_FLAG,
        want_text_param="want_text",
        notes=(
            "SAIJ download returns a LegalDocument with PDF metadata; --text "
            "(want_text) includes extracted PDF text. url/file_url come from the "
            "document."
        ),
    ),
    DocumentTextStrategy(
        source_id="infoleg",
        operation="get",
        id_params=("infoleg_id",),
        text_mode=TextMode.DIRECT,
        extra_params={"text": "original"},
        notes=(
            "Infoleg get returns norm detail; text mode 'original' (vs default "
            "'metadata') surfaces the original norm text directly in the "
            "document body."
        ),
    ),
    DocumentTextStrategy(
        source_id="csjn",
        operation="documento",
        id_params=("id",),
        text_mode=TextMode.PDF_TEXT,
        notes=(
            "CSJN documento returns page text plus extracted PDF text (via "
            "legal.pdf.extract_text) when a PDF is available. 'download' is the "
            "PDF-only alternative; documento is preferred for text + metadata."
        ),
    ),
    DocumentTextStrategy(
        source_id="ptn",
        operation="download",
        id_params=("id",),
        text_mode=TextMode.TEXT_FLAG,
        want_text_param="want_text",
        notes=(
            "PTN download returns a PDF-backed document; --text (want_text) "
            "includes extracted text. Default file_type is 'dictamen'."
        ),
    ),
    DocumentTextStrategy(
        source_id="sentencias-scba",
        operation="get",
        id_params=("code", "id"),
        text_mode=TextMode.DIRECT,
        notes=(
            "Sentencias SCBA get returns HTML detail text directly (no captcha, "
            "no PDF). The 'pdf' op is captcha-gated and PDF-backed; 'get' is the "
            "text-bearing, credit-free choice for document text."
        ),
    ),
    DocumentTextStrategy(
        source_id="pjn-juris",
        operation="download",
        id_params=("id", "document_id"),
        text_mode=TextMode.PDF_TEXT,
        notes=(
            "PJN jurisprudencia download returns PDF attachment metadata; the "
            "attachment bytes must be extracted to text (legal.pdf.extract_text) "
            "by a later step — this op has no ready want_text flag, so text "
            "extraction wiring is added when the MCP fetch path is implemented."
        ),
    ),
)

_RESOLVERS: dict[str, DocumentTextResolver] = {
    strategy.source_id: DocumentTextResolver(strategy=strategy) for strategy in _STRATEGIES
}


def _validate_registry_alignment() -> None:
    """Fail fast if a strategy drifts from the registry it must reuse.

    Guards the design invariant: every mapped source/operation must exist in
    ``legal.registry`` so the MCP tool dispatches a real seam, never a guess.
    ``resolve_operation`` raises a ``usage_error`` for unknown source/op pairs.
    """
    for source_id, resolver in _RESOLVERS.items():
        info = SOURCE_BY_ID.get(source_id)
        if info is None:
            raise usage_error(
                f"document-text strategy references unknown source: {source_id}",
                details={"source": source_id},
            )
        # Validates source AND operation against the registry/adapter surface.
        resolve_operation(source_id, resolver.operation)


_validate_registry_alignment()


def supported_document_text_sources() -> tuple[str, ...]:
    """Return the source ids with a document-text resolver, in registry order."""
    return tuple(_RESOLVERS.keys())


def get_document_text_resolver(source_id: str) -> DocumentTextResolver | None:
    """Return the document-text resolver for ``source_id`` or ``None``.

    ``None`` signals an unsupported source; callers surface that with
    :func:`document_text_error` as a normalized error envelope rather than
    raising. Supported sources return a ready :class:`DocumentTextResolver`
    bound to the existing operation the MCP tool will dispatch.
    """
    return _RESOLVERS.get(source_id)


def document_text_error(source_id: str) -> LegalResponse:
    """Build the normalized error envelope for an unsupported document source.

    Mirrors the pipeline's error shape (``ok: false`` + a ``LegalError``) so the
    MCP document-text tool returns the same envelope keys the CLI/API produce.
    Distinguishes an unknown source id from a known source that simply has no
    document-text mapping yet.
    """
    known = source_id in SOURCE_BY_ID
    message = (
        f"source '{source_id}' has no document-text resolver"
        if known
        else f"unknown source: {source_id}"
    )
    error = usage_error(
        message,
        details={
            "source": source_id,
            "supported_sources": list(supported_document_text_sources()),
        },
    ).to_error()
    return LegalResponse.error_response(
        source=source_id,
        operation="get_document_text",
        error=error,
        request={"source": source_id},
    )
