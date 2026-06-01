# SAIJ jurisprudencia y normativa

Status date: 2026-05-30

## Source

- Domain: `www.saij.gob.ar`
- Human entry point: `https://www.saij.gob.ar/`
- Recommended CLI mode: direct HTTP against the JSON search endpoint.
- Browser required: no.
- Captcha/auth: none observed.

## Search API

SAIJ advanced pages build `/resultados.jsp?...`; the result page then calls:

`GET https://www.saij.gob.ar/busqueda`

Observed parameters:

- `q`: free query in some flows
- `r`: raw query expression
- `o`: offset
- `p`: page size
- `f`: facets, pipe-separated
- `s`: sort
- `v`: view mode, usually `colapsada` or `detallada`
- `b=avanzada` for advanced-search flows

The UI `QueryObject` sends exactly those parameters to `/busqueda`. Pagination is offset based: page `n` starts at `o=(n-1)*p`.

Example:

```text
/busqueda?r=texto:%20despido&o=0&p=2&f=Total|Tipo de Documento|Fecha|Organismo|Tribunal|Publicacion|Tema|Estado de Vigencia|Autor|Jurisdiccion&v=colapsada
```

## Raw-query fields

Useful advanced-search mappings:

- Fallos: facet `Tipo de Documento/Jurisprudencia`; raw fields `titulo`, `fecha-rango:[YYYYMMDD TO YYYYMMDD]`, `tema`, `texto`, `id-infojus`.
- Sumarios: facet `Tipo de Documento/Jurisprudencia/Sumario`.
- Legislacion: facet `Tipo de Documento/Legislacion/<tipo>`; raw fields `numero`, `fecha`, `titulo`, `tema`, `texto`, `id-infojus`.
- Dictamenes: facet `Tipo de Documento/Dictamen[/PTN/INADI/MPF/OA/AAIP]`; raw fields `numero`, `fecha`, `tema`, `partes`, `letra`, `tomo`, `pagina`, `texto`, `id-infojus`.

The UI JavaScript contains the raw-query builder in `busqueda-avanzada.js` and `query-object.js`; prefer matching those strings rather than inventing a new syntax.

Useful preset entry points map to facets in `query-object.js`:

- `/buscador/jurisprudencia-corte-suprema`: `Tipo de Documento/Jurisprudencia` + `Tribunal/CORTE SUPREMA DE JUSTICIA DE LA NACION`
- `/buscador/jurisprudencia-nacional`: `Tipo de Documento/Jurisprudencia` + `Jurisdicción/Nacional`
- `/buscador/jurisprudencia-federal`: `Tipo de Documento/Jurisprudencia` + `Jurisdicción/Federal`
- `/buscador/jurisprudencia-provincial`: `Tipo de Documento/Jurisprudencia` + `Jurisdicción/Local`
- `/buscador/resoluciones-igj`: `Organismo/IGJ`
- `/buscador/dictamenes`: `Tipo de Documento/Dictamen/PTN`
- `/buscador/dictamenes-aaip`: `Tipo de Documento/Dictamen/AAIP`

Facet drilldown syntax is `Facet/Child/Subchild[siblings,depth]`, joined by `|` in `f`.

## Result/detail parsing

The `/busqueda` JSON response includes:

- `queryObjectData`: echo of `facets`, `offset`, `pageSize`, `query`, `rawQuery`, `sortBy`, and `viewType`.
- `searchResults.categoriesResultList`: facet tree.
- `searchResults.documentResultList`: current page of hits.
- `searchResults.iterationToken`, `expandedQuery`, and `inputQuery`.

For total hits, follow the UI's `getTotal(data)` logic:

`searchResults.categoriesResultList[0].facetChildren[0].facetHits`

Do not rely on `searchResults.totalSearchResults`; in the observed response it matched the current page size.

Each item in `documentResultList` contains:

- `uuid`
- `documentScore`
- `explain`
- `documentAbstract`: JSON encoded as a string

Parse `documentAbstract` as JSON. It contains `document.metadata` and `document.content`. Common metadata keys:

- `uuid`
- `document-content-type`, for example `sumario`
- `friendly-url.subdomain`
- `friendly-url.description`

Detail UI links use:

`/documentDisplay.jsp?guid=<parsed_urn>`

For search results that already expose `document.metadata.uuid`, the UUID works as the `guid`. The display page then calls:

`GET https://www.saij.gob.ar/view-document?guid=<guid>`

That endpoint returns JSON with `guid` and `data`, where `data` is another JSON string containing the full `document`. For CLI use, call `/view-document` directly and parse `data`. If a full document contains an attachment field such as `adjunto_pdf`, download its `/descarga-archivo?...` URL.

## CLI surface

Suggested command:

```bash
legal saij search --text despido --type fallo --offset 0 --limit 25
legal saij search --type sumario --text despido
legal saij search --type legislacion --numero 27430
legal saij facets --raw-query "texto: despido"
legal saij get --guid <guid>
```

Expose `--raw-query` and `--facets` for precise reproduction of UI searches.

## Verification

Calling `/busqueda` with `r=texto:%20despido`, offset `0`, page size `2`, and standard facets returned JSON whose Total facet reported `8918` hits and whose `documentResultList` contained two sumario records. `GET /documentDisplay.jsp?guid=123456789-0abc-defg8033-000esoiramus` loaded the display shell, and `GET /view-document?guid=123456789-0abc-defg8033-000esoiramus` returned a full JSON document with `document-content-type=sumario`.
