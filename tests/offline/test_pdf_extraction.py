"""Offline regression tests for PDF text extraction accent fidelity."""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

import legal.pdf as pdf

# The accent classes we must preserve through extraction.
ACCENTS = ["ñ", "ó", "í", "á"]


def _make_pdf_with_accents(tmp_path: Path) -> bytes:
    """Build a tiny PDF containing Spanish accents, without new deps.

    Uses ps2pdf (ghostscript) over a small PostScript document. Skips when
    ps2pdf is unavailable.
    """

    ps2pdf = shutil.which("ps2pdf")
    if ps2pdf is None:
        pytest.skip("ps2pdf not available to build the accented PDF fixture")

    # ISOLatin1Encoding lets us render accented glyphs via octal escapes in a
    # standard PostScript font. The bytes here are the Latin-1 code points for
    # the accented characters we want to round-trip.
    text = "El nino compro una resolucion \361 \363 \355 \341 \351 \372 \374 \277 \241"
    ps = (
        "%!PS-Adobe-3.0\n"
        "/Helvetica findfont dup length dict begin\n"
        "  { 1 index /FID ne { def } { pop pop } ifelse } forall\n"
        "  /Encoding ISOLatin1Encoding def\n"
        "  currentdict\n"
        "end\n"
        "/Helvetica-Latin1 exch definefont pop\n"
        "/Helvetica-Latin1 findfont 18 scalefont setfont\n"
        "72 700 moveto\n"
        f"({text}) show\n"
        "showpage\n"
    )
    ps_path = tmp_path / "sample.ps"
    pdf_path = tmp_path / "sample.pdf"
    ps_path.write_text(ps, encoding="latin-1")

    subprocess.run(
        [ps2pdf, str(ps_path), str(pdf_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return pdf_path.read_bytes()


def test_extract_text_preserves_spanish_accents(tmp_path: Path) -> None:
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext not available; primary engine cannot be exercised")

    pdf_bytes = _make_pdf_with_accents(tmp_path)
    text = pdf.extract_text(pdf_bytes)

    for accent in ACCENTS:
        assert accent in text, f"missing accent {accent!r} in extracted text: {text!r}"
    assert "�" not in text, f"replacement char present in extracted text: {text!r}"


def test_extract_with_pdftotext_pins_utf8_and_drops_ignore() -> None:
    """The old silent-drop failure mode is closed in the source itself."""

    source = inspect.getsource(pdf._extract_with_pdftotext)
    assert "-enc" in source, source
    assert "UTF-8" in source, source
    assert 'errors="ignore"' not in source, source
    assert "errors='ignore'" not in source, source
    assert "replace" in source, source
