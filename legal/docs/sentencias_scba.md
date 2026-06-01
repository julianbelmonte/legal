# Sentencias SCBA

Status date: 2026-05-31

## Source

- Domain: `sentencias.scba.gov.ar`
- Human entry point: `https://sentencias.scba.gov.ar/`
- Recommended CLI mode: direct JSON/HTML endpoints for lookup, search, detail,
  PDF, and anonymization.
- Browser required: no for search/detail.
- Captcha/auth: Google reCAPTCHA v3 is loaded. Search and HTML detail work
  without a token. PDF/anonymize solve reCAPTCHA v3 internally through the
  configured Capsolver key; callers do not pass a token.

## Lookup data

Register ids:

- `1`: `REGISTRO DE SENTENCIAS`
- `2`: `REGISTRO DE RESOLUCIONES`

Organism select endpoint:

`GET https://sentencias.scba.gov.ar/RegistroElectronico/OrganismosDeUnRegistro?idRegistro=<1|2>`

It returns HTML `<select>` options. Example observed: `238 = JUZGADO EN LO CIVIL Y COMERCIAL N 1 - LA PLATA`.

## Search workflow

Search endpoint:

`POST https://sentencias.scba.gov.ar/RegistroElectronico/BuscarRegistrosPorFechaYOrganismo`

JSON body:

```json
{
  "fDesde": "01/01/2026",
  "fHasta": "30/05/2026",
  "texoIncluido": "",
  "idOrganismo": "238",
  "idRegistro": "1",
  "nombreOrganismo": "JUZGADO EN LO CIVIL Y COMERCIAL N 1 - LA PLATA",
  "registro": "REGISTRO DE SENTENCIAS"
}
```

The misspelled key is `texoIncluido`, not `textoIncluido`.

## Detail/download parsing

HTML detail endpoint:

`POST /RegistroElectronico/ObtenerRegistroVisualizar/`

Body:

```json
{"idCodigoAcceso":"4459DD82"}
```

PDF endpoint:

`POST /RegistroElectronico/ObtenerRegistroVisualizarPdf/`

Body:

```json
{"idCodigoAcceso":"4459DD82","recaptchaToken":"<token>"}
```

Anonymization endpoint:

`POST /RegistroElectronico/abrirAnomizar/`

PDF/anonymization uses reCAPTCHA action `btndescargar`. The CLI obtains and
redacts this token internally.

`pdf` can enrich the returned document:

- `--text` extracts PDF text into `document.body` and
  `document.metadata.text`.
- `--save-pdf <path>` writes the PDF bytes and reports the path in
  `document.metadata.saved`.
- `document.metadata.pdf_bytes` records the downloaded byte count.

`search` returns normalized result rows with `idCodigoAcceso`; `get` returns
the HTML detail text directly without captcha. Use `pdf --text` when the
source PDF text is required for context.

## CLI surface

Suggested command:

```bash
legal sentencias-scba organisms --register sentencias
legal sentencias-scba search --register sentencias --organism 238 --from 2026-01-01 --to 2026-05-30 --text ""
legal sentencias-scba get --code 4459DD82
legal sentencias-scba pdf --code 4459DD82 --text --save-pdf .work/scba.pdf
legal sentencias-scba anonymize --nro-registro 123 --fecha 01/01/2026 --nro-expediente "LP-12345" --caratula "A c/ B" --organism "JUZGADO EN LO CIVIL Y COMERCIAL N 1 - LA PLATA"
```

## Verification

Fetching organisms for register `1` returned organism options. Searching 2026 records for organism `238` returned rows, including `idCodigoAcceso=4459DD82`; posting that code to `ObtenerRegistroVisualizar` returned full HTML text.
