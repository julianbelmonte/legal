# Deployment

How to run and configure the Legal Data API. The API is a thin FastAPI
consumer of the `legal` pipeline; every source/operation it exposes funnels
through the same `legal.dispatch.run_operation` seam the CLI uses, so its
responses echo the pipeline's normalized JSON envelope 1:1 with the CLI.

This document covers the repo-level deployment surface. Per-source reference
material lives under `legal/docs/` (do not confuse the two).

## Running the API

The ASGI app is `api.main:app`. Run it with uvicorn:

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

For multiple workers (each worker is independent; sources are stateless):

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080 --workers 4
```

Endpoints:

- `GET /healthz` — unauthenticated liveness probe.
- `GET /v1/sources`, `GET /v1/sources/{id}`, `GET /v1/schema` — discovery.
- `POST /v1/sources/{source_id}/{operation}` — generic, registry-driven access
  to every source/operation the CLI supports.
- `POST /v1/search` — global cross-source search.
- `POST /v1/csjn/...`, `POST /v1/saij/...` — typed ad-hoc endpoints.

`GET /openapi.json` serves the full OpenAPI schema.

## Authentication (required, fail-closed)

Every `/v1` route requires a valid API key in the `x-api-key` request header.
`/healthz` is the only unauthenticated route. The API **fails closed**: if no
keys are configured, all protected routes return HTTP 401.

Configure keys via either (or both) of these env vars; they are merged into one
accepted set:

| Env var          | Meaning                                  |
| ---------------- | ---------------------------------------- |
| `LEGAL_API_KEYS` | Comma-separated list of accepted keys.   |
| `LEGAL_API_KEY`  | Single accepted key (convenience).       |

Example:

```bash
export LEGAL_API_KEYS="prod-key-1,prod-key-2"
# or
export LEGAL_API_KEY="single-key"
```

Call with:

```bash
curl -s -X POST http://localhost:8080/v1/sources/saij/search \
  -H "x-api-key: prod-key-1" \
  -H "content-type: application/json" \
  -d '{"params": {"text": "habeas corpus", "limit": 5}}'
```

## Pipeline configuration (proxy + captcha)

Pipeline deploy toggles are read from the environment with the `LEGAL_` prefix
(see `legal/settings.py`). All have defaults, so the system runs with no env at
all (direct egress, capsolver captcha provider).

| Env var                   | Default     | Meaning                                              |
| ------------------------- | ----------- | ---------------------------------------------------- |
| `LEGAL_PROXY_ENABLED`     | `false`     | Master on/off for proxy egress.                      |
| `LEGAL_PROXY_PROVIDER`    | `none`      | Proxy backend: `none` or `floxy`.                    |
| `LEGAL_PROXY_COUNTRY`     | `us`        | Exit country for the proxy provider.                 |
| `LEGAL_CAPTCHA_PROVIDER`  | `capsolver` | Captcha-solving backend.                             |
| `LEGAL_BOTBROWSER_PROFILE`| (unset)     | Pin a specific BotBrowser `.enc` profile by name.    |

### Proxy on/off and provider swap

- **Disabled** (default): with `LEGAL_PROXY_ENABLED=false`, egress is direct at
  both seams (HTTP client and browser launcher) — byte-for-byte identical to the
  no-proxy behavior.
- **Enabled**: set `LEGAL_PROXY_ENABLED=true` and pick a provider with
  `LEGAL_PROXY_PROVIDER`. With `floxy`, the resolved proxy (US exits by default,
  overridable via `LEGAL_PROXY_COUNTRY`) is applied to both the HTTP client and
  the Playwright browser context. To swap providers, change
  `LEGAL_PROXY_PROVIDER`; no code changes are required.

### Captcha provider

Set `LEGAL_CAPTCHA_PROVIDER` to select the captcha backend (default
`capsolver`). The public captcha entry points used by adapters keep identical
signatures regardless of the active provider.

## Secret setup (env or `legal/secret.py`)

Secrets (the Capsolver API key and Floxy credentials) are **never** committed.
They resolve in this order: environment variable → `legal/secret.py` →
legacy `legal/local_config.py`.

| Secret               | Env var (preferred)        | `legal/secret.py` name |
| -------------------- | -------------------------- | ---------------------- |
| Capsolver API key    | `LEGAL_CAPSOLVER_API_KEY`  | `CAPSOLVER_API_KEY`    |
| Floxy username       | `LEGAL_FLOXY_USER`         | `FLOXY_USER`           |
| Floxy password       | `LEGAL_FLOXY_PASS`         | `FLOXY_PASS`           |

Option A — environment (recommended for containers/systemd):

```bash
export LEGAL_CAPSOLVER_API_KEY="cap-..."
export LEGAL_FLOXY_USER="floxy-user"
export LEGAL_FLOXY_PASS="floxy-pass"
```

Option B — file: copy the template and fill in real values. `legal/secret.py`
is gitignored.

```bash
cp legal/secret.example.py legal/secret.py
# edit legal/secret.py and set CAPSOLVER_API_KEY / FLOXY_USER / FLOXY_PASS
```

Only configure the secrets a deployment actually needs: a no-proxy deploy needs
no Floxy credentials; a deploy that never touches captcha-backed sources needs
no Capsolver key.

## Example configurations

### (a) No proxy + Capsolver

Direct egress, captcha solving via Capsolver. Floxy credentials are not needed.

```bash
# auth
LEGAL_API_KEY=change-me

# pipeline: direct egress, capsolver captcha
LEGAL_PROXY_ENABLED=false
LEGAL_CAPTCHA_PROVIDER=capsolver

# secret (only the captcha key is required here)
LEGAL_CAPSOLVER_API_KEY=cap-...
```

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

### (b) Floxy US proxy + Capsolver

Egress routed through Floxy with US exits, captcha via Capsolver.

```bash
# auth
LEGAL_API_KEY=change-me

# pipeline: floxy US proxy + capsolver captcha
LEGAL_PROXY_ENABLED=true
LEGAL_PROXY_PROVIDER=floxy
LEGAL_PROXY_COUNTRY=us
LEGAL_CAPTCHA_PROVIDER=capsolver

# secrets (captcha key + floxy credentials)
LEGAL_CAPSOLVER_API_KEY=cap-...
LEGAL_FLOXY_USER=floxy-user
LEGAL_FLOXY_PASS=floxy-pass
```

`docker run`-style:

```bash
docker run --rm -p 8080:8080 \
  -e LEGAL_API_KEY=change-me \
  -e LEGAL_PROXY_ENABLED=true \
  -e LEGAL_PROXY_PROVIDER=floxy \
  -e LEGAL_PROXY_COUNTRY=us \
  -e LEGAL_CAPTCHA_PROVIDER=capsolver \
  -e LEGAL_CAPSOLVER_API_KEY=cap-... \
  -e LEGAL_FLOXY_USER=floxy-user \
  -e LEGAL_FLOXY_PASS=floxy-pass \
  legal-api \
  uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

systemd drop-in (`[Service]` excerpt):

```ini
[Service]
Environment=LEGAL_API_KEY=change-me
Environment=LEGAL_PROXY_ENABLED=true
Environment=LEGAL_PROXY_PROVIDER=floxy
Environment=LEGAL_PROXY_COUNTRY=us
Environment=LEGAL_CAPTCHA_PROVIDER=capsolver
Environment=LEGAL_CAPSOLVER_API_KEY=cap-...
Environment=LEGAL_FLOXY_USER=floxy-user
Environment=LEGAL_FLOXY_PASS=floxy-pass
ExecStart=/usr/bin/uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

## Notes

- Browser/captcha-backed operations launch their own browser context per call
  and are slower and probabilistic; size worker/concurrency accordingly.
- Browser sources require the BotBrowser binary and `.enc` profiles on disk
  (vendored under `legal/vendor/`, restorable via `legal/scripts/bootstrap.py`).
