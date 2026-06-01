# CNACAF jurisprudencia

Status date: 2026-05-30

## Source

- Listed domain: `cnacaf.gov.ar`
- Current working public sources:
  - `https://jurisprudenciatfn.mecon.gob.ar/`
  - `https://api.jurisprudencia-tfn.ar`
  - `https://www.pjn.gov.ar/jurisprudencia2/consulta.php`
- Recommended CLI mode: use TFN/CNCAF API for case-level hybrid search and PJN document API for CNCAF bulletins/documents.
- Browser required: no for the TFN/CNCAF API or PJN document API.
- Captcha/auth: none observed.

## Domain status

`cnacaf.gov.ar` and `www.cnacaf.gov.ar` did not resolve/reach from the local probe. The active public jurisprudence search that explicitly includes CNCAF is the TFN site backed by `api.jurisprudencia-tfn.ar`, whose filters include `tribunales=["cncaf","tfn"]` and CNCAF sala names.

## TFN/CNCAF API

See also `tfn_jurisprudencia.md`. For CNCAF, set:

```json
"tribunales": ["cncaf"]
```

Relevant filters from `/filters`:

- Salas: `Sala I (CAF)` through `Sala V (CAF)`
- `tribunales`: `cncaf`, `tfn`
- Search text scope: `objetos` or `doctrinas`

Endpoint:

`POST https://api.jurisprudencia-tfn.ar/hybridSearch`

Body supports:

- `query`
- `search_in`
- `tribunales`
- `registro`
- `expediente`
- `caratula`
- `salas`
- `fecha_desde`
- `fecha_hasta`
- `limit`

## PJN fallback

PJN document search returns CNCAF records when filtering by terms/facets. Example search result showed:

- `dependencia`: `Camara Nacional de Apelaciones en lo Contencioso Adm.Fed.`
- `rubro`: `Jurisprudencia destacada`

Use:

`GET https://pjn-documento-api.pjn.gov.ar/api/documento/search?query=terms:<term>&page=<page>`

Then download:

`GET /api/documento/adjunto/<id>`

## CLI surface

Suggested command:

```bash
legal cnacaf search --query iva --search-in doctrinas --sala "Sala V (CAF)" --limit 20
legal cnacaf pdf --fallo-id <fallo_id>
legal cnacaf pjn-search --terms despido --page 0
```

Make the TFN/CNCAF backend the default. Keep `pjn-search` as a document/bulletin fallback.

## Verification

`api.jurisprudencia-tfn.ar/filters` returned CNCAF salas and `tribunales=["cncaf","tfn"]`. The old `cnacaf.gov.ar` domain failed local resolution/reachability. PJN document search returned CNCAF PDF records through `pjn-documento-api`.

