# Jusbaires Juristeca

Status date: 2026-05-30

## Source

- Domain: `juristeca.jusbaires.gob.ar`
- Human entry points:
  - `https://juristeca.jusbaires.gob.ar/`
  - `https://juristeca.jusbaires.gob.ar/busqueda-avanzada-de-jurisprudencia/`
- Recommended CLI mode: direct GET against the advanced-search WordPress page.
- Browser required: no for search/detail/PDF; browser only helps inspect dynamic descriptor autocomplete.
- Captcha/auth: none observed.

## Search workflow

The advanced form uses `method=GET` and renders results into an iframe named `XXX`, but the submitted URL is still the same page. WordPress redirects the public path to:

`/buscador-juristeca/busqueda-avanzada-de-jurisprudencia/`

Hidden/action behavior:

- Hidden `accion=buscar` by default.
- Submit JS changes `accion` to:
  - `fallos` if `buscar-fallo` or `FallosPlenarios` is checked.
  - `sumarios` if `buscar-sumario` is checked.
  - `buscar` if neither is checked.

Main GET parameters:

- `accion`: `buscar`, `fallos`, `sumarios`, `sumario`, `sumarios-del-fallo`, `temas`, `temas-fallos`
- `buscar-fallo`
- `buscar-sumario`
- `FallosPlenarios=2`
- `Fuero[]`: `1`, `2`, `3`
- `Sala[]`: observed ids `1`, `2`, `3`, `4`, `5`, `6`, `8`, `9`, `10`, `11`
- `FechaFalloDesde`: `YYYY-MM-DD`
- `FechaFalloHasta`: `YYYY-MM-DD`
- `cuerpo[]`: all text terms
- `cuerpoOR[]`: any text terms
- `cuerpoNOT[]`: excluded text terms
- `DescriptoresAND[]`
- `DescriptoresOR[]`
- `DescriptoresNOT[]`
- `descriptorPalabraIncluir[]`
- `descriptorPalabraExcluir[]`
- `Actor`
- `Demandado`
- `NumeroCausa`
- `id_fallo[]`
- `sumario[]`

Descriptor autocomplete:

`GET /baj/?accion=descriptores&q=<query>`

Returns JSON with `options` for Selectize.

## Result/detail parsing

Fallos results include:

- Result rows under `#listadogeneral`
- PDF links such as `https://juristeca.jusbaires.gob.ar/fallos/<id>.pdf`
- Descriptor links `?accion=temas-fallos&DescriptoresAND[]=<id>`
- Sumaries-for-fallo links `?accion=sumarios-del-fallo&id=<fallo_id>`

Sumario results include:

- Short and full text blocks (`contenido-corto`, `contenido-completo`)
- Sumario detail links `?accion=sumario&id=<sumario_id>`
- Related fallo links `?accion=sumarios-del-fallo&id=<fallo_id>`

## CLI surface

Suggested command:

```bash
legal jusbaires search --kind fallos --text despido
legal jusbaires search --kind sumarios --text despido --fuero 2 --sala 3 --from 2025-01-01
legal jusbaires descriptors --q penal
legal jusbaires fallo --id 62510
legal jusbaires sumario --id 102285
```

For `fallo --id`, fetch `/fallos/<id>.pdf`. For `sumario --id`, fetch the advanced page with `accion=sumario&id=<id>`.

## Verification

Fetching `?accion=fallos&buscar-fallo=on&cuerpo[]=despido` returned fallo results and PDF links such as `/fallos/62510.pdf`. Fetching `?accion=sumarios&buscar-sumario=on&cuerpo[]=despido` returned sumario result text and detail links like `?accion=sumario&id=102285`.

