# PTN dictamenes

Status date: 2026-05-31

## Source

- Domain: `busquedadictamenes.ptn.gob.ar`
- API domain: `https://api.ptn.gob.ar`
- Human entry point: `https://busquedadictamenes.ptn.gob.ar/`
- Recommended CLI mode: direct API for search and protected file download.
- Browser required: no for search.
- Captcha/auth: search is public. File confirmation/download requires
  reCAPTCHA v3, solved internally through the configured Capsolver key. Admin
  endpoints require auth.

## Frontend config

The SPA exposes:

- API base: `https://api.ptn.gob.ar`
- Novedades: `https://novedadesdictamenes.ptn.gob.ar`
- reCAPTCHA key: `6LckcgYaAAAAABSSzWzlfmJcP2YbfC6scSodMGC6`

## Search API

Endpoint:

`POST https://api.ptn.gob.ar/search?historico=<bool>&solo_historico=<bool>`

The SPA issues an axios request with `Content-Type: application/json` and
`data: JSON.stringify(e.body)`. Body is Elasticsearch-like JSON. The UI searches
these fields:

- `voces`
- `organismo`
- `attachments.attachment.content`
- `array_leyes`
- `array_decretos`
- `tomo`
- `pagina`
- `fecha`
- `numero`
- `expediente`

The UI boosts matches in `voces`, `organismo`, `array_leyes`, and `array_decretos`. Legal references such as `ley 26076` are mapped to `array_leyes`.

Common aggregations:

- `organismo.keyword`
- `voces.keyword`
- `tomo.keyword` plus `pagina`
- `anio` from date script
- `mes`
- `indices`

Advanced synthetic query text may be formatted like:

`BusquedaAvanzada: tomo ...; pagina ...; dictamen ...; fecha ...; tema ...`

Response shape is Elasticsearch-style:

- top-level keys: `took`, `timed_out`, `_shards`, `hits`, `aggregations`
- total count: `hits.total.value`
- records: `hits.hits[]`
- record identifiers: `hits.hits[]._index` and `hits.hits[]._id`
- primary metadata: `_source.numero`, `_source.tomo`, `_source.pagina`,
  `_source.fecha`, `_source.expediente`, `_source.organismo[]`,
  `_source.voces[]`
- extracted text: `_source.attachments[].attachment.content`
- file selectors: `_source.attachments[].file_type`; observed values are
  `dictamen` and `doctrina`
- snippets: `highlight`, commonly keyed by `voces` and
  `attachments.attachment.content`
- sort values: `sort[]`
- observed aggregations for a plain text search: `indices`, `organismo`,
  `tomo`, `voces`

## File download

The frontend calls `executeRecaptcha(<file_type>)` and then:

`POST https://api.ptn.gob.ar/confirmToken`

The SPA sends these as URL query parameters via axios `params`:

- `token`
- `id`
- `type`
- `historical`

The response contains a `file` path, opened as `API_BASE + file`. Use the search
hit `_id` for `id` and the attachment `file_type` for `type`.

The implemented CLI runs the reCAPTCHA v3 solve internally. There is no
`--recaptcha-token` or token environment argument. Configure
`CAPSOLVER_API_KEY` in `apps/legal/local_config.py` or export
`LEGAL_CAPSOLVER_API_KEY`.

`download` can enrich the returned document:

- `--text` extracts text from the downloaded PDF into `document.body` and
  `document.metadata.text`.
- `--save-pdf <path>` writes the PDF bytes and reports the path in
  `document.metadata.saved`.
- `document.metadata.pdf_bytes` records the downloaded byte count.

## CLI surface

Suggested command:

```bash
legal ptn search --text "ley 26076" --historico true --solo-historico false --limit 10
legal ptn search --tomo 337 --pagina 138
legal ptn download --id yUhAQJ4BRScrB8-3EmGa --type dictamen --historical false --text --save-pdf .work/ptn.pdf
```

Expose `--raw-body` for exact Elasticsearch JSON while stabilizing higher-level flags.

Search responses already include extracted attachment content in source fields,
snippets/highlights, and raw fields when requested. Use `download --text` when
the complete source PDF text is required for context.

## Verification

Posting a direct search for `ley 26076` with `historico=true&solo_historico=false` returned `hits.total.value=866`; the first hit was `_index=dictamenes`, `_id=yUhAQJ4BRScrB8-3EmGa`, with `_source.tomo=337`, `_source.pagina=138`, `_source.fecha=2026-05-18`, `_source.numero=IF-2026-49634390-APN-PTN`, `_source.expediente=EX-2025-112427651- -APN-DPC#`, attachment `file_type` values `dictamen` and `doctrina`, and highlight keys `voces` and `attachments.attachment.content`.
