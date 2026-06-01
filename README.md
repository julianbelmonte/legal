# legal

A standalone system for Argentina legal research data sources. A reusable
**data pipeline** (the `legal` package) is consumed by a **CLI**
(`legal.cli`) and a **FastAPI HTTP API** (`api`). Every source is an abstract
entity accessed through source-agnostic operations (`search`, `get`,
`download`, `pdf`, `facets`, ...), plus typed endpoints for CSJN and SAIJ and a
global cross-source search. All access returns the same normalized JSON
envelope.

See [`AGENTS.md`](AGENTS.md) for the full guide (architecture, per-source
operational notes, configuration reference).

## Quickstart

Requires Python `>=3.13`. Dependencies and the virtualenv are managed with
[`uv`](https://docs.astral.sh/uv/).

### Install

```bash
uv sync
uv run python legal/scripts/bootstrap.py   # vendors BotBrowser + .enc profiles
```

### Run the CLI

```bash
uv run python -m legal.cli sources --pretty
uv run python -m legal.cli saij search --text "despido" --limit 5 --pretty
```

If the console script is installed, `legal sources --pretty` works too. Every
command prints one JSON document to stdout (a normalized error envelope on
failure).

### Run the API

```bash
LEGAL_API_KEY=dev-key uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

Then call it with the `x-api-key` header (`/healthz` is the only open route;
OpenAPI docs at `/docs`):

```bash
curl -s -X POST http://localhost:8080/v1/sources/saij/search \
  -H "x-api-key: dev-key" -H "content-type: application/json" \
  -d '{"params": {"text": "despido", "limit": 5}}'
```

Key endpoints: `POST /v1/sources/{id}/{op}` (generic), `GET /v1/sources` and
`GET /v1/schema` (discovery), `POST /v1/search` (global), and typed
`POST /v1/csjn/*` and `POST /v1/saij/*`.

### Configure proxy / captcha

All toggles default to safe, offline-friendly values. Set them via the
environment:

```bash
export LEGAL_PROXY_ENABLED=true       # default false (direct egress)
export LEGAL_PROXY_PROVIDER=floxy     # none | floxy
export LEGAL_PROXY_COUNTRY=us
export LEGAL_CAPTCHA_PROVIDER=capsolver
```

Secrets (`CAPSOLVER_API_KEY`, `FLOXY_USER`, `FLOXY_PASS`) are never committed.
Provide them via the environment (`LEGAL_<NAME>`) or copy the template:

```bash
cp legal/secret.example.py legal/secret.py   # gitignored; then fill in values
```

### Run tests

```bash
uv run pytest -m "not live"               # offline tier: fast, no network/credentials
LEGAL_LIVE=1 uv run pytest -m live        # live tier: real data, spends credits
```
