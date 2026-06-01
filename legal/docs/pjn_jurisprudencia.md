# PJN jurisprudencia federal

Status date: 2026-05-30

## Source

- Domain: `www.pjn.gov.ar`
- Document API: `https://pjn-documento-api.pjn.gov.ar`
- Human entry point: `https://www.pjn.gov.ar/jurisprudencia2/consulta.php`
- Recommended CLI mode: direct JSON API.
- Browser required: no for the current document-search API.
- Captcha/auth: none observed for public jurisprudence/document search and PDF download.

## Current app/API

`/jurisprudencia2/consulta.php` serves an Angular app. Its production config sets:

```json
{
  "documentoApi": "https://pjn-documento-api.pjn.gov.ar"
}
```

The app calls:

- `GET /api/documento/search?query=<query>&page=<page>`
- `GET /api/documento/search/filter?query=<query>`
- `GET /api/documento/adjunto/<document_id>`

The `adjunto` endpoint returns the attached file, usually PDF.

## Scope warning

The document API is broader than jurisprudence. An empty search returned `totalElements: 144643`, with first-page records such as `Currículums postulantes Concurso 265/2010` under `Comisión de Selección de Magistrados` and `application/zip` attachments. Do not treat unfiltered `/api/documento/search` as a jurisprudence-only feed.

Use `/api/documento/search/filter?query=` to discover top-level dependencies before narrowing. Empty-query dependency facets observed:

- `2`: Consejo de la Magistratura
- `142`: Jurado de Enjuiciamiento de Magistrados de la Nación
- `151`: Fueros con Competencia en todo el País
- `210`: Fueros Nacionales
- `935`: Fueros Federales

For jurisprudence-oriented harvesting, default to `Fueros Nacionales`, `Fueros Federales`, or a user-provided dependency/rubro facet.

## Query syntax

The Angular client builds a comma-separated `query` string. Current live API
verification on 2026-05-31 showed these token names/formats:

- Full-text terms: `terms:<urlencoded text>`
- Dependency facet: `depend:<id>`
- Rubro/subrubro facet: `rubro:<id>` or `subrubro:<id>`
- Date exact/range: `fecha><ddmmyyyy>,fecha<<ddmmyyyy>` for ranges
- Number/year pair: `num:<number>,anio:<year>`
- Number only: `num:<number>`
- Year only: `anio:<year>`

Sort is appended inside the `query` parameter by the client:

- `&sort=desde,desc` for most recent
- `&sort=desde,asc` for oldest
- `&sort=orden,desc`
- `&sort=orden,asc`

In a CLI, URL-encode the whole `query` value. Pages are zero-based.

## Result parsing

Search returns Spring-style JSON with `content`, `totalElements`, `size`, `number`, etc. Each document item includes:

- `id`
- `descripcion`
- `desde`, `publicacion`, `fecha`
- `tipoAdjunto`
- `rubro`
- `dependencia`
- `palabrasClaves`
- `tipoDesc`, `numero`, `anio`

Download URL:

`https://pjn-documento-api.pjn.gov.ar/api/documento/adjunto/<id>`

## Old endpoint note

The legacy `http://jurisprudencia.pjn.gov.ar/documentos/jurisp/index.jsp` timed out from this host. Treat it as legacy/fallback only; prefer `jurisprudencia2`.

## CLI surface

Suggested command:

```bash
legal pjn-juris search --terms despido --page 0 --sort recent
legal pjn-juris facets --terms despido
legal pjn-juris search --terms despido --dependencia 935 --sort recent
legal pjn-juris download --id 211319 --out boletin.pdf
```

The first implementation can pass raw facet ids. Add `facets` to discover dependency/rubro ids before narrowing, and avoid broad empty searches unless the caller explicitly asks for portal-wide documents.

## Verification

`GET /api/documento/search?query=terms:despido&sort=desde,desc&page=0` returned JSON results including CNCAF and labor jurisprudence PDF records. `GET /api/documento/search/filter?query=terms:despido&sort=desde,desc` returned dependency facets including `Fueros Federales`. Empty search/filter calls confirmed the portal-wide scope and dependency ids above. `GET /api/documento/adjunto/172631` returned `application/pdf`.
