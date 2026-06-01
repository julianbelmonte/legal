# CSJN jurisprudencia

Status date: 2026-05-31

## Source

- Domains: `sj.csjn.gov.ar`, `sjconsulta.csjn.gov.ar`, `sjservicios.csjn.gov.ar`
- Human entry points:
  - `https://sj.csjn.gov.ar/homeSJ/`
  - `https://sjconsulta.csjn.gov.ar/sjconsulta/fallos/consulta.html`
  - `https://sjconsulta.csjn.gov.ar/sjconsulta/consultaSumarios/consulta.html`
  - `https://sjservicios.csjn.gov.ar/sj/tomosFallos.do?method=iniciar`
- Implemented CLI source id: `csjn`
- Implemented operations: `fallos`, `sumarios`, `documento`, `download`
- Browser required: yes for implemented searches and document/PDF fetches.
  Use BotBrowser. Stock Chromium and plain HTTP are unreliable because the
  source combines a WAF with score-based reCAPTCHA Enterprise.
- Captcha/auth: fallos and sumarios use invisible Google reCAPTCHA Enterprise.
  The CLI lets the page mint the token natively in BotBrowser; there is no
  user-facing captcha token flag and no Capsolver spend for CSJN.

## Implemented CLI

From the repo root:

```bash
uv run python -m apps.legal.cli csjn fallos --texto "arbitrariedad" --limit 5 --retries 3 --pretty
uv run python -m apps.legal.cli csjn sumarios --texto "amparo" --limit 5 --retries 3 --pretty
uv run python -m apps.legal.cli csjn documento --id <idDocumento> --pretty
uv run python -m apps.legal.cli csjn download --id <idDocumento> --text --save-pdf .work/csjn.pdf --pretty
```

With the console script installed, the same operations are:

```bash
legal csjn fallos --texto "arbitrariedad" --limit 5 --retries 3 --pretty
legal csjn sumarios --texto "amparo" --limit 5 --retries 3 --pretty
legal csjn documento --id <idDocumento> --pretty
legal csjn download --id <idDocumento> --text --save-pdf .work/csjn.pdf --pretty
```

`fallos` flags:

- `--texto`
- `--partes`
- `--fecha-desde`
- `--fecha-hasta`
- `--terms {todas,algunas,frase,cercanas}`
- `--retries`
- `--show`
- shared output flags: `--limit`, `--raw`, `--pretty`

`sumarios` flags:

- `--texto`
- `--retries`
- `--show`
- shared output flags: `--limit`, `--raw`, `--pretty`

`documento` fetches the document page for a known numeric `idDocumento` and also
tries to fetch/extract the associated PDF text through the browser context.
`download` is the PDF-focused operation. Use `--text` to include extracted PDF
text in the JSON response and `--save-pdf <path>` to write the raw bytes.

## BotBrowser and Enterprise scoring

The implemented search path runs through `apps/legal/browser.py` and launches
BotBrowser with a hidden display by default. Pass `--show` only for debugging.

The page owns the reCAPTCHA Enterprise flow. Before submitting, the adapter:

1. Opens the real CSJN form in BotBrowser.
2. Waits for page scripts to load.
3. Calls `grecaptcha.enterprise.ready` and performs a warm-up `execute` with
   action `submit`.
4. Adds small pointer movement and types the query with short delays.
5. Clicks the page's own Buscar control and waits for the `buscar.html`
   navigation.
6. Polls the JavaScript-rendered rows after navigation.

Enterprise scoring is probabilistic. `--retries` repeats the full browser
attempt when the WAF, token score, or delayed result rendering fails.

## Document and PDF URLs

Search result document ids are CSJN `idDocumento` values. The adapter normalizes
fallos records with both `doc_id` and `document_url`.

Document page route:

```text
https://sjconsulta.csjn.gov.ar/sjconsulta/documentos/verDocumentoByIdLinksJSP.html?idDocumento=<id>
```

PDF route:

```text
https://sjconsulta.csjn.gov.ar/sjconsulta/documentos/verDocumentoById.html?idDocumento=<id>
```

PDF bytes are fetched with the BotBrowser context request so the request reuses
the same cookies and WAF/browser state as the page visit.

## Access/WAF notes

Plain `curl` requests from this host to both fallos and sumarios entry pages
returned `HTTP/2 403` with a "Web Application Firewall" page:

- blocked URL: `https://sjconsulta.csjn.gov.ar/sjconsulta/fallos/consulta.html`
- blocked URL: `https://sjconsulta.csjn.gov.ar/sjconsulta/consultaSumarios/consulta.html`
- event id: `110000003`
- event type: `signature`

Treat non-browser HTTP clients as unreliable for full search and PDF retrieval.
The useful fallback is a known `idDocumento` routed through the implemented
BotBrowser `documento` or `download` operations, not token injection.

## Source details

Fallos form:

`GET https://sjconsulta.csjn.gov.ar/sjconsulta/fallos/consulta.html`

Submit route:

`POST /sjconsulta/fallos/buscar.html`

Observed controls include:

- `texto`
- `terminos`
- `partes`
- date fields
- `voces`
- voting checkboxes
- norm fields
- `formula`
- `cuestionFederal`
- `competencia`
- `tipoRecurso`
- `sentidoPronunciamiento`
- `g-recaptcha-response`

Sumarios form:

`GET https://sjconsulta.csjn.gov.ar/sjconsulta/consultaSumarios/consulta.html`

Submit route:

`POST /sjconsulta/consultaSumarios/buscar.html`

Observed controls include:

- `filter.fullText`
- `filter.terminos`
- `voces`
- `filter.autos`
- date fields
- `tomo`
- `pagina`
- `g-recaptcha-response`

The fallos and sumarios pages load Enterprise key
`6Lc9hfArAAAAANCZ9hMlXTx8j7hKz52W2tgwovXk` and submit with action `submit`.

## Other CSJN paths

Novedades uses classic captcha endpoints:

- `CaptchaService?tipo=P&...`
- `CaptchaService`

Useful novelty/detail endpoints seen in page scripts:

- `novedades/paginarNovedades.html?indice=`
- `sumarios/getSumariosHoldingByAnalisis.html?idAnalisis=`
- `documentos/verDocumentoByIdLinksJSP.html?idDocumento=`
- `fallos/getSintesisAnalisis.html?idAnalisis=`
- `documentos/verDocumentoById.html?idDocumento=`

Tomos Fallos remains a documented source route, but it is not one of the
implemented `csjn` CLI operations:

- Entry: `https://sjservicios.csjn.gov.ar/sj/tomosFallos.do?method=iniciar`
- POST route: `/sj/tomosFallos`
- Controls: `desdePagina`, `nroTomo`, `anioDesde`
- Result links: `/sj/verTomo?tomoId=<id>`
