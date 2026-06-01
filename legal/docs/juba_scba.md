# JUBA SCBA

Status date: 2026-05-30

## Source

- Domain: `juba.scba.gov.ar`
- Human entry point: `https://juba.scba.gov.ar/Buscar.aspx`
- Recommended CLI mode: ASP.NET WebForms HTTP client.
- Browser required: no, if WebForms state fields are preserved.
- Captcha/auth: none observed.

## Search workflow

The search page is ASP.NET WebForms. Fetch it first, preserve cookies, and submit all hidden state fields:

- `__LASTFOCUS`
- `__EVENTTARGET`
- `__EVENTARGUMENT`
- `__VIEWSTATE`
- `__VIEWSTATEGENERATOR`
- `__PREVIOUSPAGE`
- `__EVENTVALIDATION`

Main quick-search controls:

- `ctl00$cphMainContent$txtExpresionBusquedaRapida`
- `ctl00$cphMainContent$ddlMateria`
- `ctl00$cphMainContent$btnUnicaBusqueda=Buscar`
- `ctl00$cphMainContent$Anclar=1`
- Optional highlight/print/current-tab fields: keep defaults from the page.

Observed `ddlMateria` values:

- `Civil y Comercial`
- `Conflicto de Poderes`
- `Contencioso administrativa`
- `Enjuiciamiento de Magistrados`
- `Inconstitucionalidad`
- `Laboral`
- `Penal`
- `Todos`
- `-1`: placeholder `Seleccione...`

The browser-side validation requires non-empty search text and a materia other
than `-1`.

Pagination is also WebForms. Use `__EVENTTARGET` values for
next/previous/page-jump controls and resubmit the current state from the result
page:

- `ctl00$cphMainContent$lnkInicio`
- `ctl00$cphMainContent$lnkAnterior`
- `ctl00$cphMainContent$lnkSiguiente`
- `ctl00$cphMainContent$lnkFinal`
- `ctl00$cphMainContent$lnkIrPagina`

For `lnkIrPagina`, set `ctl00$cphMainContent$ddlPaginaResultados`. It is
zero-based and each option represents 20 results, e.g. value `0` is `1 - 20`,
value `1` is `21 - 40`.

## Result/detail parsing

After a search, the result page exposes category/bucket links:

- `ctl00$cphMainContent$lnkResultadoTextoSumario`
- `ctl00$cphMainContent$lnkResultadosVoces`
- `ctl00$cphMainContent$lnkResultadoTextoFallo`
- `ctl00$cphMainContent$lnkResultadoBusquedaOriginal`

Clicking those links is another WebForms postback that changes the active result
bucket. The observed labels were `en Texto del Sumario`, `en voces`, and
`en Texto del Fallo`, with counts in parentheses.

Each result row is under `cphMainContent_RepeaterDatosResultados_*` and includes:

- `lblCantidad_<n>` with `Resultado: <n> de <total>`
- materia, internal sumario id, voces, and sumario text
- collapsible panels for fallos coincidentes and no coincidentes
- optional `Ver Texto Completo del Fallo` links

Detail route:

`GET https://juba.scba.gov.ar/VerTextoCompleto.aspx?idFallo=<id>`

Parse full decision text from the detail HTML. Preserve `idFallo` as the canonical local identifier.

## CLI surface

Suggested command:

```bash
legal juba search --q despido --materia Todos --page 1
legal juba buckets --q despido --materia Todos
legal juba get --id-fallo 195861
```

Expose pagination but hide WebForms internals. Store per-search state if the CLI supports incremental paging.

## Verification

Posting a quick search for `despido` with `ddlMateria=Todos` returned bucket counts for `Texto del Sumario`, `voces`, and `Texto del Fallo`; the active bucket showed `Resultado: 1 de 1184` and a 20-result page selector with values `0..59`. `GET /VerTextoCompleto.aspx?idFallo=195861` returned a full-text decision page.
