# DPPJ resoluciones societario PBA

Status date: 2026-05-30

## Source

- Domain: `www.gba.gob.ar/dppj`
- Human entry point: `https://www.gba.gob.ar/dppj/legislacion`
- Recommended CLI mode: scrape static Drupal page and normalize linked Normas GBA/PDF records.
- Browser required: no.
- Captcha/auth: none observed.

## Public page structure

The DPPJ site is Drupal. The left navigation exposes:

- `/dppj/institucional`
- `/dppj/legislacion`
- `/dppj/anexos`
- `/dppj/delegaciones_del_interior`
- `/dppj/comunicaciones`
- `/dppj/contacto`

The `legislacion` page contains the active normative list. It includes:

- General laws/decrees linked to Infoleg and Normas GBA.
- A section titled `Disposiciones de la Direccion Provincial de Personas Juridicas`.
- Direct PDF links under `https://drive.mjus.gba.gob.ar/docs/dppj/...`.
- Normas GBA detail links for many dispositions.
- Normas GBA search links where a disposition has no direct detail link.

No separate machine API was observed on the DPPJ site itself.

## Extraction strategy

Fetch:

`GET https://www.gba.gob.ar/dppj/legislacion`

Parse each `<p>` in the body field. For every `<a>`:

- Keep link text as source title.
- Classify target:
  - `normas.gba.gob.ar/ar-b/...`: canonical Normas GBA detail.
  - `normas.gba.gob.ar/resultados?...`: Normas GBA query needing follow-up.
  - `drive.mjus.gba.gob.ar/docs/dppj/*.pdf`: direct DPPJ PDF.
  - `servicios.infoleg.gob.ar/...`: national law reference.
  - `drive.google.com`: external form/asset.
- Infer disposition number/year from text with patterns like `DISPOSICION 353/25`, `DISPOSICION 19/26`.

For Normas GBA targets, hand off to the Normas PBA client to fetch full metadata/text.

## CLI surface

Suggested command:

```bash
legal dppj list
legal dppj list --kind disposicion --year 2025
legal dppj get --number 353 --year 2025
legal dppj sync --out dppj_disposiciones.jsonl
```

`get` should first search parsed official links, then fall back to `normas-pba search --type disposition --number <n> --year <yyyy>`.

## Verification

The `legislacion` page returned direct links including `DISPOSICION 19/26`, `DISPOSICION 17/26`, multiple 2025 dispositions, Normas GBA detail links, and DPPJ PDF links under `drive.mjus.gba.gob.ar`.

