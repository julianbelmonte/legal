# Normas PBA

Status date: 2026-05-30

## Source

- Domain: `normas.gba.gob.ar`
- Human entry point: `https://normas.gba.gob.ar/`
- Recommended CLI mode: direct HTTP GET against rendered search URLs plus HTML/detail parsing.
- Browser required: no.
- Captcha/auth: none observed.

## Search workflow

Advanced search is a GET form:

`GET https://normas.gba.gob.ar/resultados`

The form id is `advanced_search`, method `GET`, action `/resultados`. There is
no CSRF token on the public search form.

Observed query parameters:

- `q[terms][raw_type]`: `Law`, `DecreeLaw`, `Decree`, `Resolution`, `Disposition`, `GeneralOrdinance`, `JointResolution`
- `q[terms][number]`
- `q[terms][year]`
- `q[phrase]`
- `q[without_words]`
- `q[with_some_words]`
- `q[date_ranges][publication_date][gte]`
- `q[date_ranges][publication_date][lte]`
- `q[terms][bulletin_number]`
- `q[sort]`

Sort values:

- `by_publication_date_desc`: ultimos publicados
- `by_updated_at_desc`: ultimos actualizados
- `by_match_desc`: mejor coincidencia con la busqueda
- `by_number_desc`: mayor numero
- `by_number_asc`: menor numero
- `by_year_asc`: mayor anio
- `by_year_desc`: menor anio

The reset link is:

`GET /resultados?cancel_filter=true`

## Result/detail parsing

Results link to canonical detail routes:

`/ar-b/<type>/<year>/<number>/<internal_id>`

Result cards use `.rule-card`. Parse:

- `.rule-name a`: title and canonical detail href
- `blockquote`: summary
- `.field-name` / `.field-info`: publication date and other metadata
- `.total`: page/total text

The sort form on the result page preserves the active filters as hidden
`q[...]` inputs and only changes `q[sort]`.

The detail page can expose multiple document variants:

- Original PDF, e.g. `/documentos/x6K5gSYB.pdf`
- Updated HTML, e.g. `/documentos/08OeAuZV.html`
- Fundamentos HTML, e.g. `/documentos/BMyLkT8B.html`

Detail pages expose metadata in `#rule-show`:

- `#rule-name`
- field rows under `.rule-section`
- document buttons under `.rule-download-links a`
- related rules under `a.related-rule-link`
- `Ultima actualizacion` text near the bottom

Parse the detail route fields and preserve the internal id, because number/year/type alone may not uniquely identify every record variant.

## CLI surface

Suggested command:

```bash
legal normas-pba search --type law --number 15000
legal normas-pba search --type disposition --number 292 --year 2025 --sort publication_desc
legal normas-pba get --path /ar-b/ley/2018/15000/2459 --text updated
legal normas-pba related --path /ar-b/ley/2018/15000/2459
```

Map CLI types to `raw_type`, but allow `--raw-type` passthrough for unsupported values.

## Verification

`GET /resultados?q[terms][raw_type]=Law&q[terms][number]=15000` returned a detail link `/ar-b/ley/2018/15000/2459`; the detail page exposed original PDF, updated HTML, and fundamentos HTML assets.
