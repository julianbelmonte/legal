"""Offline regression tests for PDF text extraction accent fidelity."""

from __future__ import annotations

import inspect
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

import legal.pdf as pdf

# A minimal valid PDF (no fonts/glyphs needed for engine-selection tests).
_MINIMAL_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

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


def test_extract_text_detailed_reports_pdftotext_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock the engine binary and the extraction call so the test is independent
    # of any installed PDF tooling and of the fixture's glyph content.
    monkeypatch.setattr(pdf.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(
        pdf, "_extract_with_pdftotext", lambda exe, path: "extracted via pdftotext"
    )

    result = pdf.extract_text_detailed(_MINIMAL_PDF)

    assert result.engine == "pdftotext"
    assert result.degraded is False
    assert result.text == "extracted via pdftotext"


def test_extract_text_detailed_reports_degraded_pypdf_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf, "_extract_with_pypdf", lambda pdf_bytes: "via pypdf")

    with caplog.at_level(logging.WARNING, logger="legal.pdf"):
        result = pdf.extract_text_detailed(_MINIMAL_PDF)

    assert result.engine == "pypdf"
    assert result.degraded is True
    assert result.text == "via pypdf"
    assert pdf.DEGRADED_FALLBACK_WARNING in caplog.text


def test_extract_text_matches_detailed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf, "_extract_with_pypdf", lambda pdf_bytes: "parity text")

    assert pdf.extract_text(_MINIMAL_PDF) == pdf.extract_text_detailed(_MINIMAL_PDF).text
