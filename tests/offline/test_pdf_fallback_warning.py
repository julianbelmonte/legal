"""Offline tests: the degraded pypdf fallback surfaces in the envelope warnings.

When ``pdftotext`` is unavailable, text extraction silently degrades to pypdf.
Step 03 routes that degradation into the normalized response envelope's
``warnings`` for every document operation that extracts PDF text. These tests
force the fallback (``legal.pdf.shutil.which`` -> ``None``) and drive two
representative paths with mocked source/browser calls:

- ``csjn`` ``handle_documento`` (direct ``extract_text_detailed`` call), and
- ``saij`` ``handle_download`` (the shared ``enrichment.finalize_document`` path).

Each must carry :data:`legal.pdf.DEGRADED_FALLBACK_WARNING` exactly once.
"""

from __future__ import annotations

import argparse

import pytest

import legal.pdf as pdf
from legal.models import LegalDocument, Provenance
from legal.pdf import DEGRADED_FALLBACK_WARNING
from legal.sources import csjn, saij

_PDF_BYTES = b"%PDF-1.4 fake body bytes"


@pytest.fixture(autouse=True)
def _force_degraded_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the degraded pypdf fallback without depending on installed tooling."""

    monkeypatch.setattr(pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf, "_extract_with_pypdf", lambda data: "extracted via pypdf")


# --- csjn documento (direct extract_text_detailed) -------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.url = "https://example/pdf"
        self.status = 200

    def body(self) -> bytes:
        return self._body

    def headers_dict(self) -> dict[str, str]:
        return {"content-type": "application/pdf"}

    @property
    def headers(self):  # playwright-style mapping with .items()
        return self.headers_dict()


class _FakeRequestApi:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def get(self, url: str):
        return _FakeResponse(self._body)


class _FakePage:
    def goto(self, url, wait_until=None):
        return _FakeResponse(b"")

    def wait_for_timeout(self, _ms):
        return None

    def title(self):
        return "CSJN documento 999"

    def evaluate(self, _script):
        return "page body text"


class _FakeCtx:
    def __init__(self, body: bytes) -> None:
        self.request = _FakeRequestApi(body)


class _FakeBotBrowser:
    def __init__(self, *args, **kwargs) -> None:
        self.page = _FakePage()
        self.ctx = _FakeCtx(_PDF_BYTES)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_csjn_documento_envelope_carries_degraded_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(csjn, "BotBrowser", _FakeBotBrowser)

    args = argparse.Namespace(id="999", show=False, raw=False)
    response = csjn.handle_documento(args)

    assert response.ok is True
    assert response.warnings.count(DEGRADED_FALLBACK_WARNING) == 1


# --- saij download (shared finalize_document path) -------------------------


class _FakeHttpResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.url = "https://example/saij.pdf"


class _FakeHttpClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method: str, url: str, **kwargs):
        return _FakeHttpResponse(_PDF_BYTES)


def _fake_base_document() -> LegalDocument:
    return LegalDocument(
        id="saij:abc",
        title="SAIJ doc",
        document_type="ley",
        files=[{"url": "https://example/saij.pdf", "kind": "pdf", "label": "PDF"}],
        provenance=Provenance(source_urls=["https://example/saij"]),
    )


def test_saij_download_envelope_carries_degraded_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saij, "_make_client", lambda: _FakeHttpClient())
    monkeypatch.setattr(saij, "fetch_document", lambda *, guid, client=None: object())
    monkeypatch.setattr(
        saij,
        "document_page_to_document",
        lambda document_page, *, include_raw=False: _fake_base_document(),
    )

    args = argparse.Namespace(guid="abc", want_text=True, save_pdf=None, raw=False)
    response = saij.handle_download(args)

    assert response.ok is True
    assert response.warnings.count(DEGRADED_FALLBACK_WARNING) == 1
