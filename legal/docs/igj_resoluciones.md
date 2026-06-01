# IGJ resoluciones

Status date: 2026-05-30

## Source

- Listed domain: `igj.gob.ar`
- Current public entry points:
  - `https://www.argentina.gob.ar/justicia/igj/institucional/marco-normativo`
  - `https://www.argentina.gob.ar/justicia/igj/marco-normativo-igj/resoluciones-generales-ano-2026`
  - `https://www.saij.gob.ar/buscador/resoluciones-igj`
- Recommended CLI mode: SAIJ search for searchable resolution corpus; Argentina.gob.ar static pages for official yearly lists/PDF links.
- Browser required: no.
- Captcha/auth: none observed for search/list/detail.

## Argentina.gob.ar official pages

The IGJ page is a Drupal/Argentina.gob.ar static content tree. The marco normativo page links yearly pages:

- `resoluciones-generales-ano-2026`
- `resoluciones-generales-ano-2025`
- `resoluciones-generales-ano-2024`
- `resoluciones-generales-ano-2023`
- older grouped pages such as `resoluciones-generales-ano-2022`, `resolucionesgenerales2021`, `resoluciones-ordenadas-por-numero`, and `resoluciones-ordenadas-por-tema`

Rows are ordinary HTML links, often to Infoleg detail pages or PDFs under `argentina.gob.ar/sites/default/files/...`.

The root IGJ page also links current news PDFs, e.g. `RG IGJ N 4/2026`.

Parse official yearly pages by collecting content links whose text/title matches
resolution labels, then classify targets as:

- Infoleg detail/text pages.
- Argentina.gob.ar uploaded PDFs.
- SAIJ or other external detail pages.

The marco normativo page links the current yearly pages with relative paths such
as `/justicia/igj/marco-normativo-igj/resoluciones-generales-ano-2026`.

## SAIJ searchable endpoint

SAIJ provides a dedicated UI:

`https://www.saij.gob.ar/buscador/resoluciones-igj`

It uses the same `/busqueda` JSON endpoint documented in `saij_jurisprudencia.md`. The UI constrains:

- `Organismo/IGJ`

The preset is defined in SAIJ `query-object.js` as:

```javascript
aux.putFacet('Organismo/IGJ');
```

Use browser-like headers for direct `/busqueda` calls. A plain curl without a
browser user agent returned `403 Forbidden`, while the same request with a
Chromium user agent and the IGJ SAIJ page as `Referer` returned JSON.

Example `/busqueda` parameters:

- `r=texto:sociedades`
- `o=0`
- `p=25`
- `f=Total|Tipo de Documento|Fecha|Organismo/IGJ|Publicación|Tema|Estado de Vigencia|Autor|Jurisdicción`
- `v=colapsada`

For robust CLI search, generate a SAIJ raw query/facet set and fetch `/busqueda`.

## CLI surface

Suggested command:

```bash
legal igj list --year 2026
legal igj search --text "sociedades extranjeras"
legal igj get-infoleg --id 401548
legal igj scrape-official-page --url https://www.argentina.gob.ar/justicia/igj/marco-normativo-igj/resoluciones-generales-ano-2026
```

The `list` command should parse the Argentina.gob.ar yearly page. The `search` command should use SAIJ and return Infoleg/document links where present.

## Verification

The IGJ marco normativo page returned yearly resolution links through 2026 and older groupings. `https://www.saij.gob.ar/buscador/resoluciones-igj` loaded as `SAIJ - Resoluciones IGJ`; its preset only adds `Organismo/IGJ`. A direct `/busqueda` request for `texto:sociedades` with the IGJ facet returned JSON with `queryObjectData.facets` including `Organismo/IGJ`, `total=77`, and document UUIDs such as `92110102-5000-0002-gcsr-senoiculoser`.
