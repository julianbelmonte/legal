# AAIP disposiciones datos personales

Status date: 2026-05-30

## Source

- Domain: `www.argentina.gob.ar/aaip`
- Human entry point: `https://www.argentina.gob.ar/aaip/buscador-normativa`
- Data source: public Google Sheets API.
- Recommended CLI mode: fetch Google Sheet JSON and filter locally.
- Browser required: no.
- Captcha/auth: none observed.

## Data endpoint

The Argentina.gob.ar page renders a Poncho/DataTables table from this public sheet:

```text
https://sheets.googleapis.com/v4/spreadsheets/1ssr92BY3h4nBTEaCsdaXTsZByr0W01uHvXvgpr-Yzyk/values/Hoja%202?alt=json&key=AIzaSyCq2wEEKL9-6RmX-TkW23qJsrmnFHFf5tY
```

Spreadsheet id:

`1ssr92BY3h4nBTEaCsdaXTsZByr0W01uHvXvgpr-Yzyk`

Sheet:

`Hoja 2`

## Columns

First row technical headers:

- `filtro-tipo`
- `numero`
- `descripcion`
- `filtro-categoria`
- `estado`
- `btn-ver`
- `btn-ver-mas`
- `fecha-pub-der`

Second row display labels:

- `Tipo`
- `Numero`
- `Descripcion`
- `Categoria`
- `Estado`
- `Texto`
- `Modificacion / derogacion`
- `Fecha de publicacion`

Actual records start at row 3.

## Filtering/parsing

The website filters client-side using DataTables search and select filters for:

- `filtro-tipo`
- `filtro-categoria`

For the CLI, load the sheet values and normalize each row into:

- `tipo`
- `numero`
- `descripcion`
- `categoria`
- `estado`
- `texto_url`
- `modificacion_derogacion_url`
- `fecha_publicacion_derogacion`

Many `texto_url` values point to Infoleg. Hand off to the Infoleg client for full text when needed.

## CLI surface

Suggested command:

```bash
legal aaip sync
legal aaip search --tipo Ley --numero 25326
legal aaip search --categoria "Datos personales" --estado Vigente
legal aaip get --numero 25326 --fetch-infoleg
```

Cache the sheet with ETag/Last-Modified if Google returns those headers.

## Verification

The sheet endpoint returned range `'Hoja 2'!A1:H3845`. Sample records included Ley 25326, Ley 26951, and Ley 27275 with Infoleg links.

