# Infoleg - normas nacionales

Status date: 2026-05-30

## Source

- Domain: `servicios.infoleg.gob.ar`
- Human entry point: `https://servicios.infoleg.gob.ar/infolegInternet/`
- Recommended CLI mode: direct HTTP form POST plus HTML parsing.
- Browser required: no.
- Captcha/auth: none observed.

## Search workflow

The search form posts to:

`POST https://servicios.infoleg.gob.ar/infolegInternet/buscarNormas.do;jsessionid=<session>`

Fetch the home page first and reuse cookies/session because the `jsessionid` is embedded in the form action. Submit `application/x-www-form-urlencoded`. Some static JS assets return `403` without browser-like headers; use a normal user agent and the Infoleg page as `Referer` when fetching page scripts.

Important form controls:

- `tipoNorma`
- `numero`
- `anioSancion`
- `texto`
- `dependencia`
- `diaPubDesde`, `mesPubDesde`, `anioPubDesde`
- `diaPubHasta`, `mesPubHasta`, `anioPubHasta`

For laws, do not submit `anioSancion`; the site rejects that combination with a validation message.

Observed `tipoNorma` values:

- `1`: Ley
- `2`: Decreto
- `8`: Decision Administrativa
- `3`: Resolucion
- `4`: Disposicion
- `12`: Acordada
- `11`: Acta
- `27`: Actuacion
- `17`: Acuerdo
- `5`: Circular
- `6`: Comunicacion
- `13`: Comunicado
- `20`: Convenio
- `14`: Decision
- `7`: Decreto/Ley
- `15`: Directiva
- `10`: Instruccion
- `23`: Interpretacion
- `24`: Laudo
- `18`: Memorandum
- `21`: Mision
- `16`: Nota
- `9`: Nota Externa
- `29`: Ordenanza
- `19`: Protocolo
- `28`: Providencia
- `22`: Recomendacion

Month selects use `0` for blank and `1..12` for `Ene..Dic`. `dependencia` is a large select in the home page; parse it dynamically for an agency helper. Observed examples include `3715 = AGENCIA DE ACCESO A LA INFORMACION PUBLICA`, `385 = ADMINISTRACION FEDERAL DE INGRESOS PUBLICOS`, and `518 = TRIBUNAL FISCAL DE LA NACION`.

## Paging/refine workflow

Result pages post back to:

`POST /infolegInternet/buscarNormas.do`

The result form includes:

- `desplazamiento`
- `irAPagina`

The page script `js/listarNormas.js` sets:

- `desplazamiento=AP` and `irAPagina=<n>` for an explicit page jump.
- `desplazamiento=+` for next page.
- `desplazamiento=U` for last page.
- `desplazamiento=R` for `Refinar Consulta`.

Keep the same session and post those hidden fields from the current result page. A broad search for `texto=seguridad` returned `Cantidad de Normas Encontradas: 93005` over `1861` pages and exposed page links using `submitFormConDesplazamientoAPagina('AP','2')`.

## Result/detail parsing

Search results are HTML links to:

- `/infolegInternet/verNorma.do?id=<norma_id>`

The detail page exposes canonical document assets:

- Original text: `anexos/<range>/<id>/norma.htm`
- Updated text: `anexos/<range>/<id>/texact.htm`
- Active/passive links: `/infolegInternet/verVinculos.do?modo=1&id=<id>` and `modo=2`

Parse title metadata from the detail page and then fetch `norma.htm` or `texact.htm` depending on `--text original|updated`.

## CLI surface

Suggested command:

```bash
legal infoleg search --type ley --number 27430
legal infoleg search --type resolucion --number 15 --year 2024 --agency 3715
legal infoleg search --text seguridad --page 2
legal infoleg get --id 305262 --text updated
legal infoleg links --id 305262 --mode active
```

Normalize `--type` to Infoleg numeric codes. Keep `--agency` as raw `dependencia` id, with a helper command to list agencies scraped from the home form.

## Verification

Verified a search for Ley 27430 by posting `tipoNorma=1&numero=27430` with no year. The result linked to `/infolegInternet/verNorma.do?id=305262`; that detail page linked to `norma.htm`, `texact.htm`, and both `verVinculos` modes. A broad `texto=seguridad` search confirmed the paging controls and `desplazamiento` values.
