"""Accent-fidelity stress test over a corpus of real CSJN PDFs.

Corpus-driven and offline: it reads cached PDF bytes (no network), so it stays
green in CI when no corpus is present and runs for real once
``tests/live/harvest_csjn_corpus.py`` (or a manual harvest) has populated
``LEGAL_CSJN_CORPUS`` (default ``.work/csjn_corpus``).

PRIMARY-PATH GATE (hard): every non-scanned PDF extracted via the pdftotext
engine must yield valid UTF-8 containing Spanish accented characters with zero
``U+FFFD`` replacement characters — i.e. the Latin-1 accent-drop / mojibake bug
is gone on genuine documents. The pypdf fallback's accent fidelity is measured
and reported (informational), and its loud-degradation behavior is asserted.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from legal import pdf as pdf_mod
from legal.pdf import (
    DEGRADED_FALLBACK_WARNING,
    extract_text,
    extract_text_detailed,
)

_ACCENTS = "áéíóúüñÁÉÍÓÚÜÑ¿¡"


def _corpus_dir() -> Path:
    return Path(os.environ.get("LEGAL_CSJN_CORPUS", ".work/csjn_corpus"))


def _corpus_pdfs() -> list[Path]:
    d = _corpus_dir()
    return sorted(d.glob("*.pdf")) if d.is_dir() else []


_PDFS = _corpus_pdfs()

pytestmark = pytest.mark.skipif(
    not _PDFS,
    reason=(
        "no CSJN corpus; populate LEGAL_CSJN_CORPUS (default .work/csjn_corpus) "
        "via tests/live/harvest_csjn_corpus.py to run the real-CSJN stress test"
    ),
)


def _accent_count(text: str) -> int:
    return sum(text.count(c) for c in _ACCENTS)


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext (poppler) not installed")
@pytest.mark.parametrize("pdf_path", _PDFS, ids=lambda p: p.stem)
def test_primary_path_preserves_accents(pdf_path: Path) -> None:
    """pdftotext extraction: valid UTF-8, Spanish accents present, no U+FFFD."""
    result = extract_text_detailed(pdf_path.read_bytes())
    assert result.engine == "pdftotext"
    assert result.degraded is False
    text = result.text

    # Image-only scans (no text layer) are out of scope (OCR): skip, don't fail.
    if len(text.strip()) < 200:
        pytest.skip(f"{pdf_path.name}: little/no text layer (likely a scanned PDF)")

    # Hard gate: the bug's signatures must be absent on real CSJN rulings.
    assert "�" not in text, f"{pdf_path.name}: U+FFFD replacement char present"
    assert _accent_count(text) > 0, (
        f"{pdf_path.name}: no Spanish accented characters in {len(text)} chars "
        "(accent stripping regression)"
    )
    # extract_text() must agree with the detailed result (API parity).
    assert extract_text(pdf_path.read_bytes()) == text


def test_fallback_is_loud_and_fidelity_reported(monkeypatch, caplog) -> None:
    """Force the pypdf fallback over the corpus: it must be loud, and we report
    its accent fidelity vs the pdftotext primary path."""
    primary_total = 0
    fallback_total = 0
    sampled = 0

    # Force the degraded path by hiding pdftotext from the engine selector.
    for pdf_path in _PDFS[:8]:  # a sample is enough; full corpus is redundant here
        data = pdf_path.read_bytes()
        if shutil.which("pdftotext"):
            primary_total += _accent_count(extract_text(data))

        monkeypatch.setattr(pdf_mod.shutil, "which", lambda _name: None)
        with caplog.at_level("WARNING", logger="legal.pdf"):
            degraded = extract_text_detailed(data)
        monkeypatch.undo()

        assert degraded.engine == "pypdf"
        assert degraded.degraded is True
        fallback_total += _accent_count(degraded.text)
        sampled += 1

    assert sampled > 0
    # Loud: the canonical degraded warning must have been logged.
    assert any(DEGRADED_FALLBACK_WARNING in rec.message for rec in caplog.records), (
        "degraded pypdf fallback did not log DEGRADED_FALLBACK_WARNING"
    )
    # Informational: surface the fidelity gap (visible with -s / on failure).
    print(
        f"\n[fidelity] sampled={sampled} accents pdftotext≈{primary_total} "
        f"pypdf≈{fallback_total}"
    )
