# PJN expedientes

Status date: 2026-05-31

## Source

- Domain: `scw.pjn.gov.ar`
- Human entry point: `https://scw.pjn.gov.ar/scw/home.seam`
- Implemented CLI source id: `pjn-expedientes`
- Implemented operations: `expediente`, `parte`, `rh`, `camaras`
- Browser required: yes for the three search operations. The source is a
  JSF/RichFaces app with a public image captcha widget.
- Captcha/auth: PJN captcha for sitekey `SCW`. The CLI solves the image
  challenge via OCR/Capsolver internally and retries on misses. Keep the real
  provider key in `apps/legal/local_config.py` or `LEGAL_CAPSOLVER_API_KEY`;
  do not put it in docs, examples, or committed files.

## Implemented CLI

From the repo root:

```bash
uv run python -m apps.legal.cli pjn-expedientes camaras --pretty
uv run python -m apps.legal.cli pjn-expedientes expediente --camara 10 --numero 12345 --anio 2024 --retries 3 --pretty
uv run python -m apps.legal.cli pjn-expedientes parte --camara 10 --role ACTOR --parte "PEREZ" --retries 3 --pretty
uv run python -m apps.legal.cli pjn-expedientes rh --nombre JUAN --apellido PEREZ --retries 3 --pretty
```

With the console script installed:

```bash
legal pjn-expedientes camaras --pretty
legal pjn-expedientes expediente --camara 10 --numero 12345 --anio 2024 --retries 3 --pretty
legal pjn-expedientes parte --camara 10 --role ACTOR --parte "PEREZ" --retries 3 --pretty
legal pjn-expedientes rh --nombre JUAN --apellido PEREZ --retries 3 --pretty
```

`camaras` returns the chamber/jurisdiction ids accepted by `--camara`.

`expediente` flags:

- `--camara`
- `--numero`
- `--anio`
- `--retries`
- `--show`
- shared output flags: `--raw`, `--pretty`

`parte` flags:

- `--camara`
- `--role`
- `--parte`
- `--retries`
- `--show`
- shared output flags: `--raw`, `--pretty`

`rh` flags:

- `--nombre`
- `--apellido`
- `--retries`
- `--show`
- shared output flags: `--raw`, `--pretty`

Search operations return JSON with `ok`, the normalized query, attempt count,
parsed result table rows, result links, `no_results`, and provenance. They are
browser observations from the public SCW site, not full docket downloads.

## Captcha flow

The implemented adapter in `apps/legal/sources/pjn_expedientes.py` opens
`home.seam` in BotBrowser, fills the chosen public search tab, then solves the
PJN widget:

1. Find the `captcha-frame` iframe.
2. Start the image challenge.
3. Read the embedded challenge image data URI.
4. Send the normalized base64 image to `apps.legal.captcha.solve_image`, which
   uses Capsolver `ImageToTextTask` synchronously.
5. Click the challenge input, clear it, and type the OCR answer with real
   Playwright keystrokes.
6. Click the accept button.
7. Poll the parent page for `#captcha-response`.
8. Refresh and retry when OCR or token minting fails.

Important gotcha: do not set the captcha input value through DOM assignment.
The widget ignores DOM-set values. The answer must be entered with real
keystrokes (`locator.type(...)`) for the widget to mint the parent token.

Capsolver image tasks return `status: "ready"` and `solution.text` from
`createTask`; do not poll `getTaskResult` for image captcha tasks.

## Application structure

The page is JSF/RichFaces/PrimeFaces. It includes:

- `javax.faces.ViewState`
- JSF AJAX helpers (`jsf.js`, `richfaces.js`, `primefaces.js`)
- PJN captcha script: `https://captcha.pjn.gov.ar/api/init.js?sitekey=SCW`
- Captcha widget: `<div class="pjn-captcha" data-sitekey="SCW">`

Main form id:

`formPublica`

Public tabs:

- `porExpediente`
- `porParte`
- `porRH`

Fetch `home.seam` first and keep `JSESSIONID` plus the `TS...` cookies. The
page action is sessionized, for example:

`/scw/home.seam;jsessionid=<session>`

## Captcha contract

The captcha script needs browser-like headers; a plain curl without a browser
user agent was rejected by `captcha.pjn.gov.ar`. With a browser user agent and
`Referer: https://scw.pjn.gov.ar/scw/home.seam`,
`GET https://captcha.pjn.gov.ar/api/init.js?sitekey=SCW` returned JavaScript.

The script injects:

- an iframe named `captcha-frame`
- widget URL shaped like
  `https://captcha.pjn.gov.ar/api/widget.scw.<token>.html?sitekey=SCW`
- a hidden input `id="captcha-response" name="captcha-response"`

When the iframe posts a `challenge-response` message, the script writes the
token into `captcha-response`. Browser mode lets the widget populate it after
the real keystroke OCR answer.

## Tab loading

Only `porExpediente` is rendered in the initial HTML. `porParte` and `porRH`
are loaded by RichFaces partial AJAX.

Common headers:

```text
Faces-Request: partial/ajax
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
```

Load `porParte`:

```text
formPublica=formPublica
formPublica:expedienteTab-value=porParte
formPublica:camaraNumAni=
formPublica:numero=
formPublica:anio=
javax.faces.ViewState=<viewstate>
javax.faces.source=formPublica:porParte
javax.faces.partial.execute=formPublica:porParte @component
javax.faces.partial.render=@component
org.richfaces.ajax.component=formPublica:porParte
formPublica:porParte=formPublica:porParte
rfExt=null
AJAX:EVENTS_COUNT=1
javax.faces.partial.ajax=true
```

Load `porRH` similarly with `formPublica:expedienteTab-value=porRH`,
`javax.faces.source=formPublica:porRH`,
`org.richfaces.ajax.component=formPublica:porRH`, and
`formPublica:porRH=formPublica:porRH`. If `porParte` is already loaded, the
browser includes its current fields too.

The `porParte` jurisdiction select also triggers a dependent AJAX refresh for
the role selector:

```text
javax.faces.source=formPublica:camaraPartes
javax.faces.partial.event=change
javax.faces.partial.execute=formPublica:camaraPartes formPublica:camaraPartes
javax.faces.partial.render=formPublica:leyenda formPublica:tipo
javax.faces.behavior.event=change
```

The response is a JSF `<partial-response>` XML document containing `<update>`
blocks with HTML fragments and a fresh `javax.faces.ViewState`.

## Search by expediente

Observed fields:

- `formPublica:camaraNumAni`
- `formPublica:numero`
- `formPublica:anio`
- Submit: `formPublica:buscarPorNumeroButton`
- Hidden: `javax.faces.ViewState`
- JSF param: `conversationPropagation=join`
- Captcha token: `captcha-response`

Submit payload shape:

```text
formPublica=formPublica
formPublica:expedienteTab-value=porExpediente
formPublica:camaraNumAni=<jurisdiction_id>
formPublica:numero=<number>
formPublica:anio=<year>
captcha-response=<captcha_token>
javax.faces.ViewState=<viewstate>
formPublica:buscarPorNumeroButton=formPublica:buscarPorNumeroButton
conversationPropagation=join
```

Observed `formPublica:camaraNumAni` / `formPublica:camaraPartes` values:

- `0`: CSJ - Corte Suprema de Justicia de la Nacion
- `1`: CIV - Camara Nacional de Apelaciones en lo Civil
- `2`: CAF - Camara Nacional de Apelaciones en lo Contencioso Administrativo Federal
- `3`: CCF - Camara Nacional de Apelaciones en lo Civil y Comercial Federal
- `4`: CNE - Camara Nacional Electoral
- `5`: CSS - Camara Federal de la Seguridad Social
- `6`: CPE - Camara Nacional de Apelaciones en lo Penal Economico
- `7`: CNT - Camara Nacional de Apelaciones del Trabajo
- `8`: CFP - Camara Criminal y Correccional Federal
- `9`: CCC - Camara Nacional de Apelaciones en lo Criminal y Correccional
- `10`: COM - Camara Nacional de Apelaciones en lo Comercial
- `11`: CPF - Camara Federal de Casacion Penal
- `12`: CPN - Camara Nacional Casacion Penal
- `13`: FBB - Justicia Federal de Bahia Blanca
- `14`: FCR - Justicia Federal de Comodoro Rivadavia
- `15`: FCB - Justicia Federal de Cordoba
- `16`: FCT - Justicia Federal de Corrientes
- `17`: FGR - Justicia Federal de General Roca
- `18`: FLP - Justicia Federal de La Plata
- `19`: FMP - Justicia Federal de Mar del Plata
- `20`: FMZ - Justicia Federal de Mendoza
- `21`: FPO - Justicia Federal de Posadas
- `22`: FPA - Justicia Federal de Parana
- `23`: FRE - Justicia Federal de Resistencia
- `24`: FSA - Justicia Federal de Salta
- `25`: FRO - Justicia Federal de Rosario
- `26`: FSM - Justicia Federal de San Martin
- `27`: FTU - Justicia Federal de Tucuman

## Search by party

The implemented `parte` operation loads the `porParte` tab first.

Observed fields:

- `formPublica:camaraPartes`
- `formPublica:tipo`
- `formPublica:nomIntervParte`
- `formPublica:buscarPorParteButton`
- Hidden: `javax.faces.ViewState`
- Captcha token: `captcha-response`

Submit payload shape:

```text
formPublica=formPublica
formPublica:expedienteTab-value=porParte
formPublica:camaraNumAni=
formPublica:numero=
formPublica:anio=
formPublica:camaraPartes=<jurisdiction_id>
formPublica:tipo=<role>
formPublica:nomIntervParte=<party_name_uppercase>
formPublica:buscarPorParteButton=Consultar
captcha-response=<captcha_token>
javax.faces.ViewState=<viewstate>
```

Observed `formPublica:tipo` values include `ACTOR`, `AFILIADO`,
`AGRUPACION POLITICA`, `AMICUS CURIAE`, `AUTORIDAD DE MESA`, `CAUSANTE`,
`CONCURSADO`, `DAMNIFICADO`, `DEMANDADO`, `DENUNCIADO`, `DENUNCIANTE`,
`EJECUTADO/S`, `EJECUTANTE/S`, `FALLIDO`, `FUNCIONARIO PUBLICO`, `HEREDERO/S`,
`IMPUTADO`, `INCIDENTISTA`, `ONG`, `ORGANISMO PUBLICO`, `PETICIONANTE`,
`QUERELLANTE`, `REQUERIDO`, `REQUIRENTE`, `SINDICO`, `SOLICITANTE`, and
`VOLUNTARIO`. Fetch the tab and parse the select each run because the options
can be refreshed after `camaraPartes` changes.

## Search by Reparacion Historica

The implemented `rh` operation loads the `porRH` tab first.

Observed fields:

- `formPublica:nombreInterveniente`
- `formPublica:apellidoInterveniente`
- `formPublica:buscarRHButton`
- Hidden: `javax.faces.ViewState`
- Captcha token: `captcha-response`

Submit payload shape:

```text
formPublica=formPublica
formPublica:expedienteTab-value=porRH
formPublica:camaraNumAni=
formPublica:numero=
formPublica:anio=
formPublica:nombreInterveniente=<first_name_uppercase>
formPublica:apellidoInterveniente=<last_name_uppercase>
formPublica:buscarRHButton=Consultar
captcha-response=<captcha_token>
javax.faces.ViewState=<viewstate>
```

## Verification notes

The home page loaded from `scw.pjn.gov.ar` and exposed JSF state, public query
tabs, the `formPublica:buscarPorNumeroButton` submit, and the PJN captcha
script/widget with `sitekey=SCW`. Playwright captured the RichFaces partial
AJAX requests for `porParte` and `porRH`, the dependent `camaraPartes` change
request, and no-token submit payloads for expediente, parte, and Reparacion
Historica.
