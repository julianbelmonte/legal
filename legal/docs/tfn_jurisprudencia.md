# TFN jurisprudencia tributaria

Status date: 2026-05-30

## Source

- Listed domain: `tfn.gob.ar`
- Current working search UI: `https://jurisprudenciatfn.mecon.gob.ar/`
- API domain: `https://api.jurisprudencia-tfn.ar`
- Recommended CLI mode: direct JSON API.
- Browser required: no.
- Captcha/auth: none observed.

## Domain status

`tfn.gob.ar` and `www.tfn.gob.ar` did not resolve from the local probe. The current working public search is a Next.js app at `jurisprudenciatfn.mecon.gob.ar`.

## API endpoints

Frontend bundle exposed:

- `POST https://api.jurisprudencia-tfn.ar/hybridSearch`
- `GET https://api.jurisprudencia-tfn.ar/filters`
- `GET https://api.jurisprudencia-tfn.ar/searchStats`
- `GET https://api.jurisprudencia-tfn.ar/latestCases`
- `GET https://api.jurisprudencia-tfn.ar/cases/<fallo_id>/ai-summary`
- `GET https://api.jurisprudencia-tfn.ar/pdf/<fallo_id>`

Filters endpoint returned:

- `tribunales`: `cncaf`, `tfn`
- TFN salas: `A`, `B`, `C`, `D`, `E`, `F`, `G`
- CNCAF salas: `Sala I (CAF)` through `Sala V (CAF)`
- `vocalias`: `1..21`
- `competencias`: `aduanera`, `impositiva`

## Search body

Endpoint:

`POST /hybridSearch`

Body fields:

```json
{
  "query": "IVA",
  "search_in": "doctrinas",
  "tribunales": ["tfn"],
  "registro": "",
  "expediente": "",
  "caratula": "",
  "salas": [],
  "vocalias": [],
  "competencias": [],
  "fecha_desde": null,
  "fecha_hasta": null,
  "regulacion_honorarios": false,
  "limit": 20
}
```

Use `null` or omit empty dates; empty strings failed validation.

`search_in` values:

- `objetos`: hechos/objeto del caso
- `doctrinas`: sumarios/doctrina

## Result parsing

Results include:

- `fallo_id`
- `rank`
- `matched_texto`
- `objeto_texto`
- `metadata`
- `doctrinas`

PDF:

`GET https://api.jurisprudencia-tfn.ar/pdf/<fallo_id>`

## CLI surface

Suggested command:

```bash
legal tfn filters
legal tfn search --query IVA --search-in doctrinas --tribunal tfn --competencia impositiva --limit 20
legal tfn search --query "valor en aduana" --search-in objetos --tribunal tfn --competencia aduanera
legal tfn pdf --fallo-id <fallo_id>
```

## Verification

`GET /filters` returned TFN/CNCAF filters. `POST /hybridSearch` with `query=IVA`, `search_in=doctrinas`, `tribunales=["tfn"]`, null dates, and `limit=2` returned results with `fallo_id`, metadata, matched text, object text, and doctrinas.

