# Boletin Oficial PBA

Status date: 2026-05-30

## Source

- Domain: `boletinoficial.gba.gob.ar`
- Human entry point: `https://boletinoficial.gba.gob.ar/buscar`
- Recommended CLI mode: direct HTTP GET plus optional AJAX page fetches.
- Browser required: no.
- Captcha/auth: none observed.

## Search workflow

Main search endpoint:

`GET https://boletinoficial.gba.gob.ar/buscar`

Parameters:

- `search[date_gteq]`: `DD/MM/YYYY`
- `search[date_lteq]`: `DD/MM/YYYY`
- `search[section]`: blank, `OFICIAL`, `JUDICIAL`, `JURISPRUDENCIA`, `SUPLEMENTO`
- `search[words]`
- `search[sort]`: blank, `by_date_desc`, `by_date_asc`
- `commit=Buscar`

The app is Rails and sets `_consulta_boletines_provincia_session`, but public search, AJAX result pages, and PDFs worked without any login or CSRF token handling beyond ordinary cookies.

Observed form labels/options:

- Section blank = all sections.
- Sort blank = default, `by_date_desc` = `Mas recientes`, `by_date_asc` = `Menos recientes`.
- Dates are `DD/MM/YYYY` in the UI and query string.

## Result/detail parsing

The result page includes the current bulletin number/date and section boxes. Section routes:

- View PDF: `/secciones/<section_id>/ver`
- Download PDF: `/secciones/<section_id>/descargar`

Search excerpts are grouped per section. Each result group has an `.ajax-paginator` with:

- `data-link`: base AJAX URL, for example `/boletin/2026-05-29/paginas/OFICIAL?q=ministerio`
- `data-total`: number of excerpt pages available for that section

Additional hit pages are loaded through AJAX:

`GET /boletin/<yyyy-mm-dd>/paginas/<SECTION>?q=<query>&page=<n>`

The AJAX endpoint returns JSON:

```json
{"html":["<div class=\"result ajax-result\">...</div>"]}
```

The HTML contains links such as `/secciones/<section_id>/ver#page=<n>` and highlighted excerpts in `span.result-highlight`.

PDF routes:

- `/secciones/<section_id>/ver` returns `application/pdf` with `Content-Disposition: inline`.
- `/secciones/<section_id>/descargar` returns the same PDF with `Content-Disposition: attachment`.

## CLI surface

Suggested command:

```bash
legal bo-pba search --from 2026-05-29 --to 2026-05-29 --section OFICIAL --words ministerio
legal bo-pba pages --date 2026-05-29 --section OFICIAL --query ministerio --page 2
legal bo-pba section --id 14187 --download
```

Use `DD/MM/YYYY` only at the HTTP boundary; expose ISO dates in the CLI.

## Verification

Searching `29/05/2026..29/05/2026` for `ministerio` returned bulletin `N 30248 - 29/05/2026` with section ids `14187`, `14188`, and `14189`. The AJAX paginator `/boletin/2026-05-29/paginas/OFICIAL?q=ministerio&page=2` returned JSON excerpts. `HEAD /secciones/14187/ver` and `/secciones/14187/descargar` both returned `application/pdf`, differing only in inline vs attachment disposition.
