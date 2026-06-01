# Legal CLI Agent Contract

This package provides a portable JSON command line interface for autonomous agents doing Argentina legal work. Run it with Python, not a shell wrapper:

```bash
uv run python -m apps.legal.cli sources
uv run python -m apps.legal.cli schema --pretty
```

The normal CLI path does not require browser automation. Browser-required CSJN fallos/sumarios search and PJN expediente search are outside this first CLI scope.

## Source Scope

Configured direct or hybrid sources:

- `aaip`: AAIP dispositions from the public sheet, with local sync/search/get.
- `bcra`: BCRA normativa search, filters, and direct download metadata.
- `bo-nacional`: Boletin Oficial Nacional search, detail, and cached continuation.
- `bo-pba`: Boletin Oficial PBA search, bulletin pages, sections, and PDFs.
- `cnacaf`: CNACAF jurisprudence via TFN/CNCAF APIs plus PJN fallback search.
- `dppj`: DPPJ public list/search with Normas PBA handoff for details.
- `igj`: IGJ resolutions through SAIJ and official yearly pages.
- `infoleg`: Infoleg national norm search, links, detail, and stateful paging.
- `juba`: SCBA JUBA WebForms search, buckets, detail, and continuation.
- `jusbaires`: Jusbaires fallos, sumarios, descriptors, and PDF metadata.
- `normas-pba`: Buenos Aires province norms search, detail, related links, and downloads.
- `pjn-juris`: PJN jurisprudence facets, search, and attachment metadata/download.
- `ptn`: PTN dictamen search; protected file download returns an unsupported captcha error.
- `saij`: SAIJ facets, search, document detail, and discovered downloads.
- `sentencias-scba`: Sentencias SCBA organisms, search, detail; protected PDF/anonymize return unsupported captcha errors.
- `tfn`: Tribunal Fiscal filters, search, latest, summary, and PDF metadata.

Use `sources` as the source of truth for the currently exposed operations:

```bash
uv run python -m apps.legal.cli sources --pretty
```

## Command Patterns

All commands write one JSON document to stdout. `--pretty` only changes formatting.

Search one source:

```bash
uv run python -m apps.legal.cli saij search --text despido --limit 5
uv run python -m apps.legal.cli infoleg search --text "ley 27430" --limit 3
uv run python -m apps.legal.cli pjn-juris search --text despido --limit 5
```

Search multiple direct sources:

```bash
uv run python -m apps.legal.cli search --source saij --source bcra --text despido --limit-per-source 2
uv run python -m apps.legal.cli search --all-direct --text "ley 26076" --limit-per-source 1
```

Fetch detail or full records with the source-specific operation and identifiers returned by search:

```bash
uv run python -m apps.legal.cli saij get --guid <guid>
uv run python -m apps.legal.cli infoleg get --id <id>
uv run python -m apps.legal.cli juba get --id-fallo <id_fallo>
uv run python -m apps.legal.cli sentencias-scba get --code <idCodigoAcceso>
```

Inspect source-specific flags with `--help`:

```bash
uv run python -m apps.legal.cli juba search --help
uv run python -m apps.legal.cli bo-pba pages --help
```

## JSON Fields

Successful responses use the normalized envelope:

- `ok`: always `true` for a completed operation.
- `source`: source id such as `saij`, `infoleg`, or `legal` for global operations.
- `operation`: operation name such as `search`, `get`, `next`, `pdf`, or `sources`.
- `query` or `request`: normalized input arguments used for the operation.
- `items`: normalized search/list results.
- `document`: normalized detail, full text, or file metadata for one record.
- `page`: pagination state, including `limit`, `offset`, `total`, `has_more`, `next_cursor`, or `search_id` when available.
- `provenance`: source URLs, fetched URLs, fetch timestamp, source-map path, and selected raw evidence.
- `warnings`: non-fatal limitations, fallbacks, or source-specific caveats.
- `facets`: filters, buckets, counts, or per-source aggregation metadata when available.

Error responses still print JSON to stdout and return a non-zero exit code:

- `ok`: `false`.
- `error.code`: stable code such as `usage_error`, `network_error`, `source_unavailable`, `parse_error`, `unsupported_operation`, or `unsupported_captcha`.
- `error.message`: concise human-readable reason.
- `error.retryable`: whether retrying later may succeed.
- `error.capability_required`: present when a missing capability blocks the operation, for example `captcha_solver`.
- `provenance`: included when the adapter has source evidence.

## Pagination

Do not assume the first search page is exhaustive. If `page.has_more` is true, continue before making coverage claims.

There are two pagination styles:

- Stateless pagination exposes `page.next_cursor`. Pass it back to the same source operation with `--cursor`.
- Stateful pagination exposes `page.search_id`. Use the source `next` operation with `--search-id`.

Examples:

```bash
uv run python -m apps.legal.cli pjn-juris search --text despido --limit 5 --cursor <next_cursor>
uv run python -m apps.legal.cli infoleg next --search-id <search_id> --limit 5
uv run python -m apps.legal.cli juba next --search-id <search_id> --limit 5
uv run python -m apps.legal.cli bo-nacional next --search-id <search_id> --limit 5
```

Stateful flows store continuation data in a portable cache. Set `LEGAL_CACHE_DIR` when a session needs an isolated or persistent cache location.

## Snippets And Full Documents

Search snippets are triage data only. Before relying on a result for legal analysis, citation, quotation, contradiction checks, or filing-sensitive work, fetch the full document or source file metadata with the relevant `get`, `links`, `download`, `pdf`, `fallo`, `sumario`, or `summary` operation.

If a source exposes both metadata and full text, prefer full text. If the CLI returns an unsupported protected-operation error, record that limitation rather than treating the snippet as complete source authority.

## Protected Operations And Captcha

Some otherwise direct sources protect file actions with reCAPTCHA. The CLI exposes those operations now so agents can plan uniformly, but no public command requires a user-supplied captcha token.

Currently protected operations:

- `ptn download --id <id> --type <dictamen|...>` returns `ok: false`, `error.code: unsupported_captcha`, `error.capability_required: captcha_solver`.
- `sentencias-scba pdf --code <code>` returns the same unsupported captcha shape.
- `sentencias-scba anonymize --code <code>` returns the same unsupported captcha shape.

Treat `unsupported_captcha` as final for the current run. It is not retryable until an internal captcha-solving capability is added behind the same command contract.

## Correctness Rules For Agents

- Always read `provenance.source_urls`, `provenance.fetched_urls`, and `provenance.source_map` before trusting a parsed result.
- Preserve upstream ids, dates, titles, document types, URLs, and `source_fields` in downstream notes.
- Treat `warnings` as part of the result, not as optional decoration.
- On `parse_error` or `source_unavailable`, do not fabricate missing legal facts. Retry or use another source.
- For broad legal questions, search more than one source and paginate until the result set is sufficient for the task.

## Tests

Run fixture tests without live network dependency:

```bash
uv run python -m pytest tests/legal -m "not e2e" -q
```

Run one adapter fixture test:

```bash
uv run python -m pytest tests/legal/test_saij_search.py -q
```

Run live smoke tests only when live public-source access is intended:

```bash
LEGAL_LIVE_SMOKE=1 uv run python -m pytest tests/legal/e2e -m e2e -q
LEGAL_LIVE_SMOKE=1 uv run python -m pytest tests/legal/e2e/test_ptn_live.py -m e2e -q
```

For Windows shells, set `LEGAL_LIVE_SMOKE=1` with the shell's environment-variable syntax before running the same `uv run python -m pytest ...` command.
