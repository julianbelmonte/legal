# MCP Readiness

An MCP (Model Context Protocol) server is **out of scope** for this project, but
the pipeline is deliberately structured so one can be added later with **no
changes to the pipeline**. This note records the seam it should consume.

## The seam (same one the API uses)

The FastAPI consumer adds no source-access logic of its own: it discovers
sources from the registry and invokes operations through a single uniform
dispatch function. An MCP server should be added as a **sibling consumer**
(e.g. a new `mcp/` package alongside `api/`) that uses the exact same two seams:

1. **Tool enumeration** — `legal.registry.list_sources()` returns the registry
   of sources and their operations. Map each source/operation pair to an MCP
   tool (and/or expose the generic source-agnostic invocation as a single tool
   taking `source`, `operation`, and a params object).

2. **Tool invocation** — `legal.dispatch.run_operation(source, op, params)`
   runs any registered operation from a params dict and returns the same
   normalized JSON envelope the CLI and API produce. This is the single
   agnostic accessor; the MCP server marshals tool arguments into `params` and
   returns the envelope as the tool result.

For a cross-source tool, `legal.global_search.run_global_search` is available as
well (the same function the `/v1/search` endpoint calls).

## Why no pipeline changes are needed

- `run_operation` already validates the source/operation against the registry,
  synthesizes the argparse namespace, calls the handler, and normalizes errors
  into the envelope — identical behavior to the CLI and API.
- The registry is the single source of truth for what tools exist, so the MCP
  tool list stays in sync automatically as sources are added or changed.
- Captcha/proxy/secret configuration is resolved inside the pipeline from the
  same `LEGAL_*` environment (see `DEPLOYMENT.md`), so an MCP deployment is
  configured exactly like the API deployment.

## Sketch (illustrative only — do not implement here)

```python
# mcp/server.py  (future, sibling to api/)
from legal.registry import list_sources
from legal.dispatch import run_operation

def tools():
    # enumerate one tool per source/operation, or a single generic tool
    return list_sources()

def call_tool(source: str, operation: str, params: dict):
    # returns the normalized envelope, 1:1 with the CLI/API
    return run_operation(source, operation, params)
```

That is the entire integration surface: enumerate from the registry, dispatch
through `run_operation`. No new pipeline code.

---

## Surface inventory (MCP OAuth VPS deployment plan)

This section maps the planned MCP tool surface and deployment automation onto
the exact existing seams each one reuses. It is the inventory note for the
`mcp-oauth-vps-deployment` plan (step 01). It records intent only; it changes no
runtime behavior. Tools live in a future sibling package (e.g. `mcp/`) and add
no source-access logic of their own — every source touch flows through the same
pipeline functions the CLI and API use.

### MCP tool → existing seam map

| MCP tool | Reused pipeline seam | Notes |
| --- | --- | --- |
| `legal_sources` | `legal.registry.list_sources()` (also `legal.registry.SOURCES` / `SOURCE_IDS`) | Enumerate the registry-backed source/operation surface; mirrors `GET /v1/sources` and `api/routers/discovery.py`. |
| `legal_source` | `legal.registry.get_source(source_id)` → `SourceInfo.to_dict()` | Single source detail; mirrors `GET /v1/sources/{source_id}`. |
| `legal_schema` | `legal.schema.LEGAL_RESPONSE_SCHEMA` | Returns the normalized response JSON Schema; mirrors `GET /v1/schema` (`api/routers/discovery.py:59`). |
| `legal_search` | `legal.global_search.run_global_search(...)` | Cross-source fan-out; same function `POST /v1/search` (`api/routers/search.py`) calls. Returns a `LegalResponse`. |
| `legal_run_operation` | `legal.dispatch.run_operation(source, operation, params, raw=...)` | Generic guarded dispatch; same seam as `api/routers/generic.py` (`POST /v1/sources/{source_id}/{operation}`). Must reject MCP-inappropriate params (`save_pdf`/`save-pdf`, raw PDF/byte requests, filesystem output paths) before dispatch. Validation of source/op happens inside `resolve_operation`. |
| `legal_get_document_text` | `legal.dispatch.run_operation(source, <doc op>, params)` to resolve+extract text, then `legal.cache` (TTL record) + `legal.pagination` cursors | First request resolves the source-specific document/download op (e.g. `csjn documento`/`download`, `saij download`, `ptn download`, `sentencias-scba pdf`, `pjn-juris download`, `bo-pba pdf`, `tfn pdf`), which extract text via `legal.pdf.extract_text` inside the pipeline. The MCP layer must request text-only (never `save_pdf`/raw bytes), store text + `LegalDocument` metadata (id/title/date/url/file_url) in a TTL cache, and return the first text page slice + opaque cursors. |
| `legal_get_document_text_page` | `legal.cache` (load TTL record) + `legal.pagination.decode_cursor` / `make_cursor` | Cursor continuation over already-cached document text; no re-fetch. Slice the cached text by the cursor's offset/limit and emit `next_cursor`/`prev_cursor`. |
| `legal_find_in_document_text` | `legal.cache` (load TTL record) + `legal.pagination` cursors | Search within cached document text; returns matching offsets/snippets and a page slice anchored on a match, with cursor navigation. |

### Document text paging seams

- **Extraction**: pipeline document/download operations already extract PDF text
  internally via `legal.pdf.extract_text` (e.g. `legal/sources/csjn.py` uses
  `from legal.pdf import extract_text`). MCP never returns raw PDF bytes or saved
  paths — it requests text and reads `document.body` / `document.metadata` text.
- **Document metadata**: `legal.models.LegalDocument` carries `id`, `title`,
  `date`, `url`, `file_url`, `text_format`, and `metadata` — the fields the MCP
  `document` envelope must surface alongside the `text_page`.
- **TTL cache**: `legal.cache` provides the TTL-record pattern
  (`SearchCacheRecord`, `save_search_state`/`load_search_state`,
  `DEFAULT_SEARCH_TTL`, `get_cache_dir`). The MCP document-text store mirrors this
  record/TTL/atomic-write shape for cached extracted text + metadata.
- **Opaque cursors**: `legal.pagination` provides `make_cursor` / `encode_cursor`
  / `decode_cursor` / `validate_cursor` (URL-safe base64 JSON, version-checked,
  source/operation-bound). Document text pages reuse this for `next_cursor` /
  `prev_cursor`. The `text_page` fields (`start_char`, `end_char`, `total_chars`)
  and page fields (`limit`, `offset`, `total`, `has_more`) map onto offset-based
  cursor payloads (`offset`/`limit`).

### Envelope parity

MCP tool results preserve the normalized envelope keys produced by
`legal.models.LegalResponse` (`ok`, `source`, `operation`, `query`, `document`,
`page`, `provenance`, `warnings`, `error`). Generic dispatch and global search
return the response unchanged; document-text tools wrap an MCP-specific envelope
that adds `text_page` while keeping `document` metadata and the page/cursor
fields above. Parity tests assert MCP generic/search outputs match
`run_operation` / `run_global_search` (and thus the API).

### Guarded params for `legal_run_operation`

The generic tool must reject MCP-inappropriate parameters before reaching
`run_operation`, including (non-exhaustive, plus future equivalents):

- `save_pdf` / `save-pdf` and any save/output filesystem path
- raw PDF / raw byte download requests
- any param that would expose remote file writes or raw downloads

Discovery of the legitimate per-operation params comes from each operation's
`add_arguments` (the same source `legal.dispatch` builds its parser from).

### Deployment automation surfaces (added by later plan steps)

These do not exist yet; they are inventoried here so later steps reuse the right
seams and existing scripts:

- **MCP/OAuth server package** — new sibling to `api/` (e.g. `mcp/`): server,
  tools, settings, OAuth provider, metadata endpoints, bearer-protected
  transport, ASGI mount beside `api.main:app`.
- **Settings/secrets** — follows the existing `legal/settings.py` (`LEGAL_*`) and
  `api/settings.py` (`LEGAL_API_*`) patterns. New MCP/OAuth config names:
  `LEGAL_MCP_PUBLIC_URL`, `LEGAL_MCP_OAUTH_ISSUER`, `LEGAL_MCP_ALLOWED_EMAILS`,
  `LEGAL_MCP_OAUTH_SIGNING_KEY`, `LEGAL_MCP_OAUTH_LOGIN_SECRET`,
  `LEGAL_MCP_OAUTH_CLIENT_ALLOWLIST`, plus server-side `LEGAL_API_KEY`. Deploy
  secrets are read from `/home/spider/.config/legal-agent/deploy.env` and
  `~/.config/ngrok/ngrok.yml`; raw token values are never committed or printed.
- **Cloudzy automation** — new OpenAPI-backed client + deploy CLI for VPS
  provision/inspect/destroy, plus remote VPS bootstrap that reuses the existing
  `legal/scripts/bootstrap.py` to vendor BotBrowser on the VPS.
- **Deploy orchestrator + ngrok** — provisions/reuses a Cloudzy VPS, syncs the
  repo, runs `uv sync`, runs bootstrap, writes a remote env file, starts the
  API/MCP ASGI app under systemd, starts ngrok, discovers the public HTTPS URL,
  updates runtime OAuth public URL metadata, and prints
  `https://<temporary-ngrok-host>/mcp`. Supports destroy/cleanup.
- **Agent skills** — a Cloudzy deployment skill and a legal MCP deployment skill.
- **Docs** — a deployment + Claude Cowork runbook (alongside `docs/DEPLOYMENT.md`).

### Tests (added by later plan steps)

- Offline MCP tool contract tests with mocked `run_operation` / `run_global_search`.
- API/dispatch parity tests for MCP generic + search tools.
- OAuth flow tests (protected resource metadata, authorization code flow, token
  exchange, token validation, single-user rejection).
- Document text paging/search tests (no silent truncation; valid next/prev
  cursor navigation).
- Cloudzy client tests (mocked HTTP + OpenAPI schema fixtures).
- Deployment dry-run tests (render remote commands without contacting Cloudzy).
- Codex CLI remote MCP smoke tests against the deployed MCP URL.

Tests follow the existing two-tier layout: offline under `tests/offline/`
(`-m "not live"`), gated remote/live under `tests/live/` (`-m live`).
