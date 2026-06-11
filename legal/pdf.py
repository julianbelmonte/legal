"""PDF text extraction helpers for the standalone legal CLI."""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

logger = logging.getLogger("legal.pdf")

DEGRADED_FALLBACK_WARNING = (
    "pdf text extracted via degraded pypdf fallback "
    "(poppler/pdftotext missing); accent fidelity may be reduced"
)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    engine: str  # "pdftotext" or "pypdf"
    degraded: bool  # True when the pypdf fallback was used


def extract_text(data: bytes | str) -> str:
    """Extract plain text from PDF bytes or a filesystem path."""

    return extract_text_detailed(data).text


def extract_text_detailed(data: bytes | str) -> ExtractionResult:
    """Extract text and report which engine ran and whether it was degraded."""

    pdf_bytes, source_path = _read_pdf(data)
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        if source_path is not None:
            text = _extract_with_pdftotext(pdftotext, source_path)
        else:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as temp:
                temp.write(pdf_bytes)
                temp.flush()
                text = _extract_with_pdftotext(pdftotext, Path(temp.name))
        return ExtractionResult(text=text, engine="pdftotext", degraded=False)

    text = _extract_with_pypdf(pdf_bytes)
    logger.warning(DEGRADED_FALLBACK_WARNING)
    return ExtractionResult(text=text, engine="pypdf", degraded=True)


def extract_text_from_base64(b64: str) -> str:
    """Decode a base64 PDF payload and extract plain text."""

    value = b64.strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    value = "".join(value.split())
    pdf_bytes = base64.b64decode(value + "=" * (-len(value) % 4))
    return extract_text(pdf_bytes)


def _read_pdf(data: bytes | str) -> tuple[bytes, Path | None]:
    if isinstance(data, bytes):
        return data, None

    path = Path(data).expanduser()
    return path.read_bytes(), path


def _extract_with_pdftotext(pdftotext: str, path: Path) -> str:
    result = subprocess.run(
        [pdftotext, "-enc", "UTF-8", "-layout", str(path), "-"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _extract_with_pypdf(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


__all__ = [
    "DEGRADED_FALLBACK_WARNING",
    "ExtractionResult",
    "extract_text",
    "extract_text_detailed",
    "extract_text_from_base64",
]
