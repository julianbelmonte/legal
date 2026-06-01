# BCRA normativa

Status date: 2026-05-30

## Source

- Domain: `www.bcra.gob.ar`
- Search API: `https://svc-index.bcra.gob.ar`
- Human entry point: `https://www.bcra.gob.ar/buscador/`
- Recommended CLI mode: direct JSON API.
- Browser required: no.
- Captcha/auth: none observed.

## Frontend config

The BCRA search page loads a Vue plugin from:

`/wp-content/plugins/buscador-bcra-plugin/dist/assets/index-*.js`

It sets:

`bcra_config.api_url = "https://svc-index.bcra.gob.ar"`

## API endpoints

Categories:

`GET https://svc-index.bcra.gob.ar/categories`

Search:

`GET https://svc-index.bcra.gob.ar/search`

Parameters:

- `q`
- `category`
- `from_date`: `YYYY-MM-DD`
- `to_date`: `YYYY-MM-DD`
- `page`
- `size`

Category values observed:

- `Paginas`
- `Noticias`
- `Catalogo de datos`
- `Eventos`
- `Comunicaciones`
- `Textos ordenados`
- `Informes`
- `Estadisticas e indicadores`
- `Documentos`

The API advertises rate limiting, observed as `ratelimit-policy: 100;w=90`.

## Result parsing

Search returns JSON with hits and total counts. Each hit includes `_source` fields such as:

- `url`
- `title`
- `category`
- date fields/snippet metadata depending on content type

For communications, PDFs commonly live under:

`https://www.bcra.gob.ar/archivos/Pdfs/comytexord/<code>.pdf`

## CLI surface

Suggested command:

```bash
legal bcra categories
legal bcra search --q A8323 --page 1 --size 10
legal bcra search --q A8323 --category Comunicaciones
legal bcra download --url https://www.bcra.gob.ar/archivos/Pdfs/comytexord/A8323.pdf
```

Use URL encoding carefully for accented category labels. If a category-specific request returns `400`, retry without category and filter client-side.

## Verification

`GET /categories` returned the category list above. `GET /search?q=A8323&page=1&size=2` returned JSON with five hits; the first hit was a `Comunicaciones` PDF at `/archivos/Pdfs/comytexord/A8323.pdf`.

