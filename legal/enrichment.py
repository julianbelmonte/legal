"""Shared PDF response enrichment helpers for legal source adapters."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from legal.pdf import DEGRADED_FALLBACK_WARNING, extract_text_detailed


def add_text_arguments(parser: argparse.ArgumentParser) -> None:
    """Register common document enrichment flags."""

    parser.add_argument(
        "--text",
        dest="want_text",
        action="store_true",
        help="include extracted PDF text in the response",
    )
    parser.add_argument("--save-pdf", dest="save_pdf", help="optional path for writing the PDF bytes")


def finalize_document(
    pdf_bytes: bytes,
    *,
    want_text: bool,
    save_path: str | None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Save PDF bytes and optionally attach extracted text metadata.

    When ``want_text`` triggers a degraded ``pypdf`` extraction fallback and a
    ``warnings`` list is supplied, the canonical
    :data:`legal.pdf.DEGRADED_FALLBACK_WARNING` is appended (once) so callers
    can surface it in the normalized response envelope.
    """

    saved: str | None = None
    if save_path:
        path = Path(save_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf_bytes)
        saved = str(path)

    result: dict[str, Any] = {
        "pdf_bytes": len(pdf_bytes),
        "saved": saved,
    }
    if want_text:
        extraction = extract_text_detailed(pdf_bytes)
        result["text"] = extraction.text
        if extraction.degraded and warnings is not None and DEGRADED_FALLBACK_WARNING not in warnings:
            warnings.append(DEGRADED_FALLBACK_WARNING)
    return result


__all__ = ["add_text_arguments", "finalize_document"]
