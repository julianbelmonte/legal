# Boletin Oficial nacional

Status date: 2026-05-30

## Source

- Domain: `www.boletinoficial.gob.ar`
- Human entry points:
  - `https://www.boletinoficial.gob.ar/seccion/primera`
  - `https://www.boletinoficial.gob.ar/busquedaAvanzada/all`
  - `https://www.boletinoficial.gob.ar/busquedaAvanzada/busquedaEspecial`
- Recommended CLI mode: direct HTTP to the advanced-search JSON endpoints, seeded by the search page for cookies; parse returned HTML fragments and detail pages.
- Browser required: no for the current public search/detail flow. Use browser automation only for exploratory UI work.
- Captcha/auth: none observed on public search/detail pages.

## Local network caveat

From this host, normal DNS resolution failed for `www.boletinoficial.gob.ar`, `boletinoficial.gob.ar`, and `nuevaweb.boletinoficial.gob.ar`. DNS-over-HTTPS resolved `www.boletinoficial.gob.ar` and `nuevaweb.boletinoficial.gob.ar` to `200.108.150.10` / `200.108.151.10`; the apex host had no A record. Access worked with an explicit host resolve override.

The first page request sets session cookies:

- `BoletinWebSession`
- `BORA`
- `TS0101c369`

A CLI should surface DNS failures clearly, allow a configured resolver/proxy path, and keep cookies between the page seed request and JSON POSTs.

## Direct request API

Seed URL:

`GET https://www.boletinoficial.gob.ar/busquedaAvanzada/all`

Primary async endpoints:

- `POST /busquedaAvanzada/realizarBusqueda`
- `POST /busquedaAvanzada/realizarBusqueda/segunda`
- `GET /busquedaAvanzada/primera/rubros`
- `GET /busquedaAvanzada/segunda/rubros`
- `GET /busquedaAvanzada/tercera/rubros`
- `GET /calendario/dias_publicacion/busqueda/0`

The POST body is form-encoded and matches the page's jQuery calls:

- `params=<json>`
- `array_volver=[]`

Use `X-Requested-With: XMLHttpRequest`, `Accept: application/json, text/javascript, */*; q=0.01`, the search page as `Referer`, and the cookies from the seed request.

Core `params` keys:

- `texto`
- `seccion`: `[1]`, `[2]`, `[3]`, or multiple section ids
- `rubros`
- `nroNorma`, `anioNorma`
- `denominacion`, `comienzaDenominacion`, `ordenamientoSegunda`
- `tipoContratacion`, `nroContratacion`, `anioContratacion`
- `fechaDesde`, `fechaHasta` in `dd/mm/yyyy`
- `tipoBusqueda`: `Avanzada` or `Rapida`
- `numeroPagina`
- pagination state: `ultimoRubro`, `ultimaSeccion`, `ultimoItemExterno`, `ultimoItemInterno`
- booleans: `todasLasPalabras`, `busquedaOriginal`, `hayMasResultadosBusqueda`, `filtroPorRubrosSeccion`, `filtroPorRubroBusqueda`, `filtroPorSeccionBusqueda`
- `seccionesOriginales`

For section 2 only, use `/busquedaAvanzada/realizarBusqueda/segunda`; its response includes `ultimos_items` for nested continuation. Other sections use `/busquedaAvanzada/realizarBusqueda`.

Response shape:

- top-level `error`, `mensajes`, `content`
- `content.html`: rendered result fragment with result links
- `content.cantidad_result_seccion`
- `content.sig_pag`
- `content.ult_seccion`
- `content.ult_rubro` for the main endpoint
- `content.ultimos_items` for the section-2 endpoint

## Search workflow

The public advanced-search UI exposes these user-facing filters:

- Keywords, with `Todas las palabras` / `Alguna de las palabras` mode.
- `Seccion`
- `Numero`
- `Anio de norma`
- `Denominacion/Nombre`
- Denomination matching mode: `Comienza` / `Contiene`
- Ordering mode: `Denominacion-Rubro` / `Rubro-Denominacion`
- `Tipo Contratacion`
- `N Contratacion`
- `Anio`
- `Fecha Desde`
- `Fecha Hasta`

The special-search page groups results by first-section categories such as Acordadas, Avisos oficiales, Decretos, Disposiciones, Leyes, Resoluciones, Resoluciones conjuntas, Resoluciones generales, and Resoluciones sintetizadas.

The UI JavaScript is loaded from `/js/busqueda.js`. It posts the same `params` object for quick search and advanced search; quick search also sets `fecha` from the selected publication day.

## Result/detail parsing

Current first-section result/detail URLs use:

- Section list: `/seccion/primera`
- Detail: `/detalleAviso/primera/<aviso_id>/<yyyymmdd>`
- Search detail variants may append `?busqueda=<n>&suplemento=1`.

Detail pages include:

- Section heading and category.
- Title, norm type/number, GDE/document code.
- Publication city/date and full text.
- `Fecha de publicacion`.
- Sometimes a published-page PDF link hosted off-domain, for example on `s3.arsat.com.ar`.

## CLI surface

Suggested command:

```bash
legal bo-nacional search --section primera --keywords "seguridad social" --mode all --from 2026-05-29 --to 2026-05-29
legal bo-nacional special --keywords covid --category decretos --supplement true
legal bo-nacional section --section primera --date 2026-05-29
legal bo-nacional get --section primera --id 342525 --date 2026-05-29
```

Implement the first version as a JSON endpoint client. Parse `content.html` for results, then fetch detail pages directly.

## Verification

External access confirmed `busquedaAvanzada/all`, `busquedaAvanzada/busquedaEspecial`, and `/seccion/primera` on 2026-05-30. With a host resolve override, `GET /busquedaAvanzada/all` returned cookies and `POST /busquedaAvanzada/realizarBusqueda` for `seguridad social`, section 1, `29/05/2026` returned JSON with three first-section results, `sig_pag: 2`, `ult_seccion: "primera"`, and detail links such as `/detalleAviso/primera/342525/20260529?busqueda=1`. `POST /busquedaAvanzada/realizarBusqueda/segunda` for section 2 returned JSON with `ultimos_items`. A special-search result opened at `/detalleAviso/primera/5282323/20210408?busqueda=3&suplemento=1`.
