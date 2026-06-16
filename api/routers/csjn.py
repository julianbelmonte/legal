"""Typed ad-hoc CSJN router: ``/v1/csjn/{fallos,sumarios,documento,download}``.

CSJN is one of the most relevant sources, so it gets first-class typed endpoints
(explicit request models, good OpenAPI docs) in addition to the generic
``/v1/sources/{id}/{op}`` route. These endpoints are thin wrappers: each request
model is converted to a params dict (dropping ``None`` values) and handed to the
same uniform dispatch seam via ``api.runner.run("csjn", "<op>", params)``, so the
response is the unmodified normalized envelope (1:1 with the CLI).

CSJN operations are browser-backed (native reCAPTCHA Enterprise scoring, no
Capsolver spend); the runner already runs them off the event loop.

The model field names mirror the CLI option spelling expected by
``legal.dispatch`` (e.g. ``text`` for the ``--text`` flag, ``save_pdf`` for
``--save-pdf``), and only include flags the corresponding operation accepts plus
the shared ``limit``/``raw`` flags every operation supports.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import api.runner
from api.auth import auth_dependencies

router = APIRouter(prefix="/v1/csjn", tags=["csjn"], dependencies=auth_dependencies)


class FallosRequest(BaseModel):
    """Typed body for ``POST /v1/csjn/fallos`` (CSJN fallos search)."""

    texto: str | None = Field(default=None, description="free text to search in fallos")
    partes: str | None = Field(default=None, description="party-name filter")
    fecha_desde: str | None = Field(default=None, description="lower decision date bound")
    fecha_hasta: str | None = Field(default=None, description="upper decision date bound")
    terms: Literal["todas", "algunas", "frase", "cercanas"] | None = Field(
        default=None, description="CSJN term-matching mode"
    )
    retries: int | None = Field(
        default=None, description="search attempts; reCAPTCHA Enterprise scoring is probabilistic"
    )
    show: bool | None = Field(
        default=None, description="show BotBrowser instead of running under the hidden display"
    )
    limit: int | None = Field(default=None, description="maximum number of records to return")
    raw: bool | None = Field(default=None, description="include raw source fields")


class SumariosRequest(BaseModel):
    """Typed body for ``POST /v1/csjn/sumarios`` (CSJN sumarios search)."""

    texto: str | None = Field(default=None, description="free text to search in sumarios")
    tomo: str | None = Field(
        default=None,
        description="Fallos citation volume, e.g. 315 (for a 'tomo:pagina' cite like 315:2616)",
    )
    pagina: str | None = Field(
        default=None,
        description="Fallos citation page, e.g. 2616 (for a 'tomo:pagina' cite like 315:2616)",
    )
    retries: int | None = Field(
        default=None, description="search attempts; reCAPTCHA Enterprise scoring is probabilistic"
    )
    show: bool | None = Field(
        default=None, description="show BotBrowser instead of running under the hidden display"
    )
    limit: int | None = Field(default=None, description="maximum number of records to return")
    raw: bool | None = Field(default=None, description="include raw source fields")


class DocumentoRequest(BaseModel):
    """Typed body for ``POST /v1/csjn/documento`` (fetch a CSJN document)."""

    id: str | None = Field(default=None, description="CSJN idDocumento value")
    show: bool | None = Field(
        default=None, description="show BotBrowser instead of running under the hidden display"
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


class DownloadRequest(BaseModel):
    """Typed body for ``POST /v1/csjn/download`` (download a CSJN document PDF).

    Note: ``save_pdf`` writes the PDF bytes to a path **on the server** running
    the API, not to the HTTP client. Returning the PDF bytes over HTTP is out of
    scope; the normalized envelope's document/text fields are returned as-is.
    """

    id: str | None = Field(default=None, description="CSJN idDocumento value")
    text: bool | None = Field(
        default=None, description="include extracted PDF text in the response"
    )
    save_pdf: str | None = Field(
        default=None,
        description="optional server-side path for writing the PDF bytes (written on the server)",
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


@router.post("/fallos")
async def fallos(body: FallosRequest) -> JSONResponse:
    """Search CSJN fallos and return the normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("csjn", "fallos", params)


@router.post("/sumarios")
async def sumarios(body: SumariosRequest) -> JSONResponse:
    """Search CSJN sumarios and return the normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("csjn", "sumarios", params)


@router.post("/documento")
async def documento(body: DocumentoRequest) -> JSONResponse:
    """Fetch a CSJN document page + extracted PDF text; normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("csjn", "documento", params)


@router.post("/download")
async def download(body: DownloadRequest) -> JSONResponse:
    """Download a CSJN document PDF through BotBrowser; normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("csjn", "download", params)
