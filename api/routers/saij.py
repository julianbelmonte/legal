"""Typed ad-hoc SAIJ router: ``/v1/saij/{facets,search,get,download}``.

SAIJ is one of the most relevant sources, so it gets first-class typed endpoints
(explicit request models, good OpenAPI docs) in addition to the generic
``/v1/sources/{id}/{op}`` route. These endpoints are thin wrappers: each request
model is converted to a params dict (dropping ``None`` values) and handed to the
same uniform dispatch seam via ``api.runner.run("saij", "<op>", params)``, so the
response is the unmodified normalized envelope (1:1 with the CLI).

SAIJ is a direct HTTP/API source (no browser, no captcha spend).

The model field names mirror the CLI option spelling expected by
``legal.dispatch`` (the ``dest`` of each ``argparse`` flag, e.g. ``text`` for
``--text``, ``raw_query`` for ``--raw-query``, ``document_type`` for ``--type``,
``guid`` for ``--guid``/``--id``, ``want_text`` for the download ``--text``
flag). Only flags the corresponding operation accepts are included, plus the
shared ``limit``/``cursor``/``raw`` flags every operation supports.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import api.runner
from api.auth import auth_dependencies

router = APIRouter(prefix="/v1/saij", tags=["saij"], dependencies=auth_dependencies)


class SearchRequest(BaseModel):
    """Typed body for ``POST /v1/saij/search`` (SAIJ full-text search)."""

    text: str | None = Field(default=None, description="free text query mapped to SAIJ texto")
    raw_query: str | None = Field(
        default=None, description="SAIJ raw query expression passed as r"
    )
    facets: str | None = Field(default=None, description="pipe-separated SAIJ facets passed as f")
    offset: int | None = Field(default=None, ge=0, description="zero-based SAIJ search offset")
    document_type: str | None = Field(
        default=None, description="SAIJ document type preset, e.g. fallo or sumario"
    )
    sort: str | None = Field(default=None, description="SAIJ sort expression passed as s")
    limit: int | None = Field(default=None, description="maximum number of records to return")
    cursor: str | None = Field(
        default=None, description="stateless pagination cursor from a prior response"
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


class FacetsRequest(BaseModel):
    """Typed body for ``POST /v1/saij/facets`` (SAIJ search facet listing)."""

    text: str | None = Field(default=None, description="free text query mapped to SAIJ texto")
    raw_query: str | None = Field(
        default=None, description="SAIJ raw query expression passed as r"
    )
    facets: str | None = Field(default=None, description="pipe-separated SAIJ facets passed as f")
    offset: int | None = Field(default=None, ge=0, description="zero-based SAIJ search offset")
    document_type: str | None = Field(
        default=None, description="SAIJ document type preset, e.g. fallo or sumario"
    )
    sort: str | None = Field(default=None, description="SAIJ sort expression passed as s")
    limit: int | None = Field(default=None, description="maximum number of records to return")
    cursor: str | None = Field(
        default=None, description="stateless pagination cursor from a prior response"
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


class GetRequest(BaseModel):
    """Typed body for ``POST /v1/saij/get`` (fetch a SAIJ document by guid)."""

    guid: str | None = Field(
        default=None,
        description="SAIJ document guid from search results (accepts the saij:<guid> item id too)",
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


class DownloadRequest(BaseModel):
    """Typed body for ``POST /v1/saij/download`` (download a SAIJ document PDF).

    Note: ``save_pdf`` writes the PDF bytes to a path **on the server** running
    the API, not to the HTTP client. Returning the PDF bytes over HTTP is out of
    scope; the normalized envelope's document/text fields are returned as-is.
    """

    guid: str | None = Field(
        default=None,
        description="SAIJ document guid (accepts the saij:<guid> item id too)",
    )
    want_text: bool | None = Field(
        default=None, description="include extracted PDF text in the response"
    )
    save_pdf: str | None = Field(
        default=None,
        description="optional server-side path for writing the PDF bytes (written on the server)",
    )
    raw: bool | None = Field(default=None, description="include raw source fields")


@router.post("/facets")
async def facets(body: FacetsRequest) -> JSONResponse:
    """Return SAIJ search facets; normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("saij", "facets", params)


@router.post("/search")
async def search(body: SearchRequest) -> JSONResponse:
    """Search SAIJ and return the normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("saij", "search", params)


@router.post("/get")
async def get(body: GetRequest) -> JSONResponse:
    """Fetch a SAIJ document by guid; normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("saij", "get", params)


@router.post("/download")
async def download(body: DownloadRequest) -> JSONResponse:
    """Download a SAIJ document PDF attachment when present; normalized envelope."""
    params = body.model_dump(exclude_none=True)
    return await api.runner.run("saij", "download", params)
