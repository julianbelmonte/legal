# Legal Pipeline Agent Guide

Standalone system for Argentina legal research sources. A reusable **data
pipeline** (`legal` package) is consumed by a **CLI** (`legal.cli`) and a
**FastAPI HTTP API** (`api`). Every source is an abstract entity accessed
through source-agnostic operations (`search`, `get`, `download`, `pdf`,
`facets`, ...), with ad-hoc typed endpoints for the most relevant sources
(CSJN, SAIJ) and a global cross-source search. All access returns the same
normalized JSON envelope.

## Architecture

- **Data pipeline — `legal/`** (import root `legal.*`). Owns all source-access
  logic: the source registry (`registry.py`), adapters (`sources/*.py`), the
  HTTP client (`http.py`), the BotBrowser launcher (`browser.py`), the captcha
  shim (`captcha.py`), parsing/PDF/enrichment helpers, and provider abstractions
  under `legal/providers/` (captcha + proxy backends). The pipeline knows nothing
  about its consumers.
- **CLI consumer — `legal.cli`** (run via `python -m legal.cli` / `legal`).
  Prints exactly one JSON document to stdout, including a normalized error
  envelope on failure.
- **API consumer — `api/`**. A FastAPI app that imports `legal` and adds only
  request/response adaptation (auth, error→HTTP mapping, typed models). It adds
  no source-access logic of its own.
- **Shared dispatch seam — `legal.dispatch.run_operation(source_id, operation,
  params: dict, *, raw=False)`**. The single source-agnostic accessor that
  invokes any operation of any source from a parameter dictionary and returns
  the normalized envelope. Both the API and the CLI's global search reuse this
  seam.
- **Planned MCP consumer.** An MCP server is out of scope today, but the
  `legal.dispatch` seam is the ready attachment point for it — a future MCP
  consumer wraps `run_operation` the same way the API does, with no changes to
  the pipeline.

## Setup

Environment is managed with `uv` (Python `>=3.13`). Install dependencies and
vendor the browser:

```bash
uv sync
uv run python legal/scripts/bootstrap.py
```

Bootstrap vendors BotBrowser plus `.enc` profiles into `legal/vendor/` (which is
gitignored) and seeds local config when missing.

## Configuration

Deploy-time, non-secret selection flags are read from the environment with the
`LEGAL_` prefix (see `legal/settings.py`). All have safe defaults so the offline
tier needs no env:

| Env var | Default | Meaning |
| --- | --- | --- |
| `LEGAL_PROXY_ENABLED` | `false` | Enable proxy egress. With it disabled, egress is direct (today's behavior). |
| `LEGAL_PROXY_PROVIDER` | `none` | Proxy backend: `none`, `floxy`, or `anyip`. |
| `LEGAL_PROXY_COUNTRY` | `us` | Exit country for the proxy provider. |
| `LEGAL_PROXY_ROTATE_ON_FAILURE` | `true` | On a transient failure, rebuild behind a **fresh proxy exit** before retrying (HTTP client + browser launcher). A dead/stalled residential/mobile exit is abandoned, not retried. |
| `LEGAL_ANYIP_TYPE` | `mobile` | AnyIP exit pool: `mobile`, `residential`, or `datacenter`. |
| `LEGAL_CAPTCHA_PROVIDER` | `capsolver` | Captcha backend selection. |
| `LEGAL_CSJN_CAPTCHA` | `native` | CSJN reCAPTCHA mode: `native` (page scoring) or `capsolver` (inject a provider token; wired but not yet reliably better than native). |
| `LEGAL_BOTBROWSER_PROFILE` | unset | Pin a specific `.enc` profile by name/stem; otherwise the launcher rotates over all profiles. |

API auth keys (see `api/settings.py`, prefix `LEGAL_API_`):

| Env var | Meaning |
| --- | --- |
| `LEGAL_API_KEYS` | Comma-separated list of accepted API keys. |
| `LEGAL_API_KEY` | Single accepted API key (merged with `LEGAL_API_KEYS`). |

The API **fails closed**: with no keys configured, protected `/v1` routes reject
every request with 401.

### Secrets

Secret credentials are **never** committed. They are resolved (env first, then
the local module) for these names:

- `CAPSOLVER_API_KEY` — Capsolver captcha-solving API key.
- `FLOXY_USER`, `FLOXY_PASS` — Floxy proxy credentials.

Provide them either via the environment (`LEGAL_<NAME>`, e.g.
`LEGAL_CAPSOLVER_API_KEY`) which takes precedence, or by copying
`legal/secret.example.py` to `legal/secret.py` (gitignored) and filling in real
values:

```bash
cp legal/secret.example.py legal/secret.py
# then edit legal/secret.py
```

## CLI Run Commands

```bash
uv run python -m legal.cli sources --pretty
uv run python -m legal.cli schema --pretty
uv run python -m legal.cli <source> <operation> [flags]
```

If the project console script is installed:

```bash
legal sources --pretty
legal <source> <operation> [flags]
```

All commands print one JSON document to stdout. Non-zero exits still print a
normalized JSON error envelope. A run that needs Capsolver:

```bash
LEGAL_CAPSOLVER_API_KEY=<key> uv run python -m legal.cli ptn search --text "empleo publico" --limit 3
```

## Discovery

Use discovery before guessing flags:

```bash
uv run python -m legal.cli sources --pretty
uv run python -m legal.cli schema --pretty
uv run python -m legal.cli ptn search --help
uv run python -m legal.cli csjn fallos --help
uv run python -m legal.cli pjn-expedientes camaras --pretty
```

`sources` returns configured source ids and operations. `schema` returns the
normalized response schema used by search, document, and error responses.

## API

Run the HTTP API:

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

Every protected request must present a valid key in the **`x-api-key`** header.
`/healthz` is the only unauthenticated route. Interactive OpenAPI docs live at
`/docs`.

Surface:

- **Generic uniform endpoint** — `POST /v1/sources/{source_id}/{operation}`,
  body `{"params": {...}}`. Exposes every source/operation pair the registry
  supports, 1:1 with the CLI, via `legal.dispatch.run_operation`.
- **Discovery** — `GET /v1/sources`, `GET /v1/sources/{source_id}`,
  `GET /v1/schema`.
- **Global cross-source search** — `POST /v1/search` (mirrors the CLI `search`
  command).
- **Typed CSJN** — `POST /v1/csjn/{fallos,sumarios,documento,download}`.
- **Typed SAIJ** — `POST /v1/saij/{facets,search,get,download}`.

All routes return the pipeline's normalized JSON envelope. Example:

```bash
curl -s -X POST http://localhost:8080/v1/sources/saij/search \
  -H "x-api-key: $LEGAL_API_KEY" -H "content-type: application/json" \
  -d '{"params": {"text": "despido", "limit": 5}}'
```

## Tests

Two tiers (see `tests/offline/` and `tests/live/`):

```bash
# Offline tier — default, fast, no network or credentials.
uv run pytest -m "not live"

# Live tier — gated, pulls real data from every source (spends Capsolver/proxy
# credits) and runs end-to-end API checks. Requires configured secrets.
LEGAL_LIVE=1 uv run pytest -m live
```

The offline tier covers provider abstractions, proxy/captcha wiring, the
dispatch seam, and the API (auth, error mapping, routing) with mocked source
calls. The live tier proves the real sources end-to-end.

---

## Per-source Operational Notes

The detailed per-source docs live under `legal/docs/` and stay as reference.
A quick map of source ids and operations follows.

### PTN

Source id: `ptn`. Operations: `search`, `download`.

```bash
uv run python -m legal.cli ptn search --text "responsabilidad del estado" --limit 5 --pretty
uv run python -m legal.cli ptn search --numero 123 --historico true --limit 5 --pretty
uv run python -m legal.cli ptn download --id <hit_id> --type dictamen --text --save-pdf .work/ptn.pdf --pretty
```

`search` returns `items` with normalized hit metadata, snippets/source fields,
facets, provenance, and pagination. `download` returns a `document` with PDF
metadata, optional saved path, and extracted text when `--text` is present.

Captcha model: invisible reCAPTCHA v3 solved internally through Capsolver, so
this source consumes Capsolver credits when protected tokens are needed.

### Sentencias SCBA

Source id: `sentencias-scba`. Operations: `organisms`, `search`, `get`, `pdf`,
`anonymize`.

```bash
uv run python -m legal.cli sentencias-scba organisms --register sentencias --pretty
uv run python -m legal.cli sentencias-scba search --register sentencias --organism "SUPREMA CORTE" --from 2024-01-01 --to 2024-12-31 --text "amparo" --limit 5 --pretty
uv run python -m legal.cli sentencias-scba get --code <idCodigoAcceso> --pretty
uv run python -m legal.cli sentencias-scba pdf --code <idCodigoAcceso> --text --save-pdf .work/scba.pdf --pretty
```

`organisms` lists valid organism ids/names. `search` returns matching record
items and pagination. `get` returns the HTML detail as a `document` with direct
text. `pdf` returns PDF metadata, optional extracted text, and optional saved
bytes.

Captcha model: protected PDF/anonymize actions use invisible reCAPTCHA v3 via
Capsolver and consume credits.

### CSJN

Source id: `csjn`. Operations: `fallos`, `sumarios`, `documento`, `download`.

```bash
uv run python -m legal.cli csjn fallos --texto "arbitrariedad" --limit 5 --retries 3 --pretty
uv run python -m legal.cli csjn sumarios --texto "amparo" --limit 5 --retries 3 --pretty
uv run python -m legal.cli csjn documento --id <idDocumento> --pretty
uv run python -m legal.cli csjn download --id <idDocumento> --text --save-pdf .work/csjn.pdf --pretty
```

`fallos` and `sumarios` return search `items` when the browser score is accepted.
`documento` returns page text plus extracted PDF text when available. `download`
returns a PDF-backed `document`, optional saved path, and extracted text with
`--text`.

CSJN caps a result set at 5000 rows. A query that matches more (e.g. a bare
`--texto "amparo"`) returns `ok: true`, `accepted: true`, but empty `items` and a
`warnings` entry telling you to narrow the query — this is **not** a rejected
captcha score. Add terms or restrict dates/court and retry until the set drops
under the cap.

Captcha model: CSJN uses invisible reCAPTCHA Enterprise. The CLI uses
BotBrowser/native page execution and retries for score acceptance; it does not
spend Capsolver credits.

### PJN Expedientes

Source id: `pjn-expedientes`. Operations: `camaras`, `expediente`, `parte`,
`rh`.

```bash
uv run python -m legal.cli pjn-expedientes camaras --pretty
uv run python -m legal.cli pjn-expedientes expediente --camara 2 --numero 12345 --anio 2024 --retries 3 --pretty
uv run python -m legal.cli pjn-expedientes parte --camara 2 --role ACTOR --parte "PEREZ" --retries 3 --pretty
uv run python -m legal.cli pjn-expedientes rh --nombre JUAN --apellido PEREZ --retries 3 --pretty
```

`camaras` returns jurisdiction ids. Search operations return JSON with `ok`,
the query, attempt count, parsed result rows, result links, `no_results`, and
provenance. These are browser observations, not normalized full case files.

Captcha model: PJN shows a visible image captcha in its browser widget. The CLI
solves the image through OCR/Capsolver and retries when the answer or search is
not accepted, so it consumes Capsolver credits.

### Direct Sources

Most other sources are direct HTTP or cached API sources and do not need the
browser/captcha path. See `sources` for the current list, including `aaip`,
`bcra`, `bo-nacional`, `bo-pba`, `cnacaf`, `dppj`, `igj`, `infoleg`, `juba`,
`jusbaires`, `normas-pba`, `pjn-juris`, `saij`, and `tfn`.

```bash
uv run python -m legal.cli search --all-direct --text "ley 26076" --limit-per-source 1 --pretty
uv run python -m legal.cli saij search --text "despido" --limit 5 --pretty
uv run python -m legal.cli infoleg get --id <id> --pretty
uv run python -m legal.cli pjn-juris search --text "danos" --limit 5 --pretty
```

### Context Enrichment

Use `--text` to include extracted text for PDF-backed document operations and
`--save-pdf` to write the PDF bytes:

```bash
uv run python -m legal.cli csjn download --id <idDocumento> --text --save-pdf .work/csjn.pdf --pretty
uv run python -m legal.cli sentencias-scba pdf --code <idCodigoAcceso> --text --save-pdf .work/scba.pdf --pretty
uv run python -m legal.cli ptn download --id <hit_id> --text --save-pdf .work/ptn.pdf --pretty
```

Direct text: PTN search can expose attachment content/snippets, and Sentencias
SCBA `get` returns HTML detail text directly. PDF extraction: CSJN `documento`
or `download`, PJN jurisprudence downloads, Sentencias SCBA `pdf`, and PTN
`download` require PDF bytes before full extracted text is available.

### General Operational Notes

- Browser-backed sources need a display. By default the CLI runs BotBrowser in a
  hidden Xvfb display; pass source-specific `--show` only when you need to see
  the browser.
- Browser and captcha sources are slower and probabilistic. Use `--retries`
  where available and treat retryable JSON errors as source-state evidence.
- Capsolver credits are used by PJN Expedientes, PTN, and Sentencias SCBA.
  CSJN defaults to BotBrowser/native Enterprise scoring (no Capsolver); set
  `LEGAL_CSJN_CAPTCHA=capsolver` to inject a provider token instead.
- CSJN is a hostile target: its gateway returns intermittent WAF 502s and its
  reCAPTCHA Enterprise scores automated browsers low, so per-attempt success is
  variable on **every** egress (mobile/residential/VPN/direct). Reliability comes
  from proxy-exit rotation + a generous `--retries` budget; on exhaustion it
  returns a clean **retryable** error (the agent re-calls) rather than hanging.
- Proxy egress is optional and off by default; when enabled it is applied at
  both egress seams (HTTP client and browser launcher). On a transient failure
  each seam rotates to a fresh exit (`LEGAL_PROXY_ROTATE_ON_FAILURE`) so a dead
  residential/mobile exit is abandoned, not retried.
- The pipeline is standalone — do not add imports from `drone` or depend on
  repo-only runtime services from other projects.
