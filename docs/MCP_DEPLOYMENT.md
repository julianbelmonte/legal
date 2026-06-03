# MCP Deployment Runbook

End-to-end runbook for the legal MCP server: local development, OAuth
configuration, Cloudzy VPS provisioning, ngrok public URLs, remote smoke
testing, Claude Cowork connector setup, troubleshooting, and teardown.

The deliverable of a deploy is a single HTTPS **MCP URL** (ending in `/mcp`)
that you paste into a Claude Cowork custom connector. The connector
authenticates over OAuth, restricted to a single allowed email.

> Never print, log, or commit raw token values. The deploy tooling redacts
> secrets in all of its output; you must do the same.

---

## 1. What gets deployed

A single uvicorn process serves the combined ASGI app (`api.main:app`) with
three surfaces behind one port:

- `/healthz` — unauthenticated liveness probe.
- `/v1/*` — the existing HTTP API, protected by the `x-api-key` header
  (`LEGAL_API_KEY` / `LEGAL_API_KEYS`).
- `/mcp` — the bearer-protected MCP streamable-HTTP transport. Unauthenticated
  calls receive `401` + `WWW-Authenticate`.
- OAuth discovery + flow endpoints (`/.well-known/*` and `/oauth/*`), reachable
  without a bearer token, wired to the single-user OAuth provider.

The MCP server exposes a compact, read-only surface of **8 tools**:

| Tool | Purpose |
| --- | --- |
| `legal_sources` | Enumerate wired source ids and operations. |
| `legal_source` | Describe one source. |
| `legal_schema` | Return the normalized response schema. |
| `legal_search` | Global cross-source search. |
| `legal_run_operation` | Invoke any source/operation pair from a params dict. |
| `legal_get_document_text` | Retrieve extracted document text. |
| `legal_get_document_text_page` | Paginated read of document text. |
| `legal_find_in_document_text` | Search within a retrieved document's text. |

---

## 2. Local MCP testing

You can run the MCP transport two ways.

**Combined app (recommended)** — serves `/healthz`, the `/v1` API, the `/mcp`
transport, and the OAuth endpoints in one process:

```bash
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

**Standalone MCP app** — only the MCP transport, OAuth routes, and `/healthz`
(binds `127.0.0.1:8081` by default; override with `LEGAL_MCP_HOST` /
`LEGAL_MCP_PORT`):

```bash
uv run python -m mcp_server.main
```

For local development without OAuth, disable auth so the MCP secrets are not
required:

```bash
LEGAL_MCP_AUTH_ENABLED=false uv run python -m mcp_server.main
```

Run the offline test tier (no network, no credentials):

```bash
uv run pytest -m "not live"
```

---

## 3. OAuth env vars and single-user allowlisting

The MCP server reads its OAuth/runtime config from the environment with the
`LEGAL_MCP_` prefix (see `mcp_server/settings.py`). It **fails closed**: when
auth is enabled (the default), the signing key and login secret must be set or
the server refuses to start.

| Env var | Meaning |
| --- | --- |
| `LEGAL_MCP_PUBLIC_URL` | Public HTTPS URL of the MCP transport (the `/mcp` endpoint); also the OAuth resource/audience. |
| `LEGAL_MCP_OAUTH_ISSUER` | OAuth issuer (the tunnel base URL). Empty derives it from `LEGAL_MCP_PUBLIC_URL`. |
| `LEGAL_MCP_ALLOWED_EMAILS` | Comma-separated allowlist of emails permitted to authenticate. For single-user access, set exactly one address. |
| `LEGAL_MCP_OAUTH_SIGNING_KEY` | Secret used to sign issued OAuth access tokens (required when auth is on). |
| `LEGAL_MCP_OAUTH_LOGIN_SECRET` | Secret gating the single-user login form (required when auth is on). |
| `LEGAL_MCP_OAUTH_CLIENT_ALLOWLIST` | Comma-separated allowlist of OAuth client ids. Empty leaves dynamic client registration open. |
| `LEGAL_API_KEY` | Accepted key for the `x-api-key`-protected `/v1` API routes. |

**Single-user allowlisting:** set `LEGAL_MCP_ALLOWED_EMAILS` to the one email
you will log in with during the OAuth flow. Any other email is rejected at the
consent step, so the MCP server is effectively single-user even though dynamic
client registration is open.

During deploy, the orchestrator discovers the ngrok URL and writes
`LEGAL_MCP_PUBLIC_URL` (the `/mcp` endpoint) and `LEGAL_MCP_OAUTH_ISSUER` (the
tunnel base) into the remote env file so the OAuth metadata advertises the live
public URL.

---

## 4. Deploy secrets

The deploy tooling resolves local credentials through a redaction-safe loader
(`legal_deploy/secrets.py`). Provide them once on the machine that runs the
deploy:

- `CLOUDZY_API_TOKEN` — Cloudzy API token, read from the deploy env file
  (default `~/.config/legal-agent/deploy.env`, overridable via
  `LEGAL_DEPLOY_ENV_FILE`). The env var `CLOUDZY_API_TOKEN` takes precedence
  over the file.
- `NGROK_AUTHTOKEN` — ngrok authtoken. Read from the `NGROK_AUTHTOKEN`
  environment variable first, then from the ngrok config file (default
  `~/.config/ngrok/ngrok.yml`, overridable via `NGROK_CONFIG_FILE`).

Keep the deploy env file at `0600`; the loader warns about loose permissions.

```bash
mkdir -p ~/.config/legal-agent
printf 'CLOUDZY_API_TOKEN=<token>\n' > ~/.config/legal-agent/deploy.env
chmod 600 ~/.config/legal-agent/deploy.env
```

---

## 5. Cloudzy VPS provisioning

Low-level VPS catalog and lifecycle operations are wrapped by the Cloudzy CLI
(`python -m legal_deploy.cloudzy_cli`). Every subcommand prints one JSON
document; `--dry-run` is a global flag that contacts no network and needs no
token. The `CLOUDZY_API_TOKEN` is never printed.

```bash
# Discovery (read-only)
python -m legal_deploy.cloudzy_cli regions
python -m legal_deploy.cloudzy_cli products
python -m legal_deploy.cloudzy_cli os
python -m legal_deploy.cloudzy_cli ssh-keys
python -m legal_deploy.cloudzy_cli instances

# Provision (always dry-run first)
python -m legal_deploy.cloudzy_cli provision \
  --region us-east --product vps-1 --hostname legal-agent \
  --ssh-key <key-id> --wait --dry-run

# Tear down a specific instance
python -m legal_deploy.cloudzy_cli destroy <instance_id> --dry-run
```

For routine infrastructure work, use the **`cloudzy-deployment`** agent skill
(`agent_skills/cloudzy-deployment`), which wraps this CLI.

---

## 6. ngrok temporary URLs

Until a permanent domain + certificate exist, the VPS uses ngrok to expose a
public, trusted HTTPS URL pointing at the combined app. The bootstrap installs
ngrok and runs `ngrok http <port>` as a systemd unit; the deploy then queries
ngrok's local agent API for the running tunnel's https `public_url` and appends
`/mcp` to form the MCP URL.

The ngrok authtoken is resolved from `NGROK_AUTHTOKEN` (env) or the ngrok config
file. The authtoken is configured on the VPS out-of-band (`ngrok config
add-authtoken`) and never appears in logged output.

> ngrok URLs are **temporary**: a free tunnel changes on restart. After any
> restart of the ngrok service, re-run the deploy to rediscover the URL and
> refresh `LEGAL_MCP_PUBLIC_URL` / `LEGAL_MCP_OAUTH_ISSUER`.

---

## 7. End-to-end deploy

The orchestrator (`python -m legal_deploy.deploy`) composes everything:
provision-or-reuse a VPS, wait for SSH, rsync the repo, write the remote env
file (`chmod 600`), run the bootstrap, `uv sync`, start the systemd services,
verify `/healthz`, and discover the ngrok URL. Recorded state lives at
`~/.config/legal-agent/deploy-state.json`.

```bash
# Inspect the ordered plan safely (no network, no token, no SSH)
python -m legal_deploy.deploy --dry-run --json

# Real deploy
python -m legal_deploy.deploy --json

# Provision a brand-new instance instead of reusing recorded state
python -m legal_deploy.deploy --fresh --json
```

The deploy result includes `instance_id`, `ip`, `ngrok_url`, `mcp_url`,
`api_health`, and `next_steps`. The `mcp_url` (e.g. `https://<ngrok>/mcp`) is
what you hand to Claude Cowork.

For the end-to-end application deploy, use the **`legal-mcp-deployment`** agent
skill (`agent_skills/legal-mcp-deployment`), which wraps this orchestrator.

---

## 7b. Production deploy: fixed domain + Caddy (always-on)

The ngrok orchestrator above is for ephemeral validation. The **always-on
production deployment** runs behind a stable domain (`mcp.arglegal.live`) with
automatic HTTPS from Caddy + Let's Encrypt, and the AnyIP Argentina proxy
enabled for the browser sources. It is fully reproducible from any machine with
the deploy secrets via a single script:

```bash
# Render the plan + artifacts (Caddyfile, systemd unit, env keys); no SSH.
legal_deploy/deploy_domain.sh --host <ip> --dry-run

# Deploy to an existing SSH-reachable host (defaults: domain mcp.arglegal.live,
# app dir /opt/legal-agent, service user legal, port 8080, email yoli@arglegal.live).
legal_deploy/deploy_domain.sh --host <ip>
```

The script is idempotent and safe to re-run. It:

1. rsyncs the repo into `/opt/legal-agent` (excludes `.git`/`.venv`/`.work`/`vendor`),
2. renders the remote `/opt/legal-agent/.env` (chmod 600) from fixed, non-secret
   config (public URL, issuer, `LEGAL_PROXY_*=anyip/ar`) plus secrets sourced
   from the local deploy env file — secrets are never printed,
3. remotely installs system deps, `uv`, **Caddy** (official apt repo), the
   service user, runs `uv sync`, and best-effort vendors BotBrowser,
4. installs the `legal-api` systemd unit (`uvicorn api.main:app`) and a
   `/etc/caddy/Caddyfile` reverse-proxying `<domain>` → `127.0.0.1:<port>`,
5. reloads both services and health-checks the public domain
   (`/healthz`, `/icon.png`, `/.well-known/oauth-protected-resource` → 200;
   `/mcp` → 401 after the 307 → `/mcp/` redirect).

**Secrets** come from a local KEY=VALUE file (default
`~/.config/legal-agent/deploy.env`, chmod 600 — see
`legal_deploy/deploy.env.example`). Required: `LEGAL_ANYIP_USER`,
`LEGAL_ANYIP_PASS`, `LEGAL_CAPSOLVER_API_KEY`, `LEGAL_MCP_OAUTH_SIGNING_KEY`,
`LEGAL_MCP_OAUTH_LOGIN_SECRET`, `LEGAL_API_KEY`. Keep the signing key / login
secret **stable** across redeploys or previously issued bearer tokens and the
Claude Cowork connector authorization stop validating.

**DNS is a prerequisite the script does not manage.** The domain's A record must
already point at the host (managed locally via the `namecheap-domains` skill);
the script warns if `<domain>` does not resolve to `--host`, because Caddy
cannot issue a certificate until it does.

The resulting MCP URL is the stable `https://mcp.arglegal.live/mcp` — it does
**not** rotate (unlike ngrok), so the Claude Cowork connector URL is permanent.

---

## 8. Remote smoke test

Before configuring Claude Cowork, exercise the deployed `/mcp` endpoint from
this machine through the Codex CLI. The smoke writes a temporary Codex MCP
config (bearer token redacted in all reportable output), lists the MCP tools,
calls `legal_sources`, and runs a small `legal_search`.

```bash
# Plan only (no network, no token, no codex binary needed)
python -m legal_deploy.smoke_codex --server-url https://<ngrok>/mcp --dry-run

# Live smoke
python -m legal_deploy.smoke_codex --server-url https://<ngrok>/mcp
```

A bearer token, when needed, is read from `--bearer-token` or
`LEGAL_MCP_BEARER_TOKEN`. On failure the result lists the diagnostics to
collect (service logs, ngrok status, OAuth metadata, the `/mcp` 401 challenge).

---

## 9. Claude Cowork connector setup

1. Run the deploy and copy the `mcp_url` from its output
   (`https://<ngrok>/mcp`).
2. In **Claude Cowork**, add a **custom MCP connector** and paste that URL.
3. Claude Cowork performs the OAuth flow: it discovers the authorization server
   from the well-known metadata, registers a client, and opens the single-user
   login/consent form.
4. Log in with the email you placed in `LEGAL_MCP_ALLOWED_EMAILS`, supplying the
   login secret (`LEGAL_MCP_OAUTH_LOGIN_SECRET`). Any other email is rejected.
5. Once authorized, the 8 `legal_*` tools become available to the agent.

If the ngrok URL changes, repeat the deploy and update the connector URL in
Claude Cowork.

---

## 10. Troubleshooting

- **Service logs** — on the VPS, inspect the systemd journal for the app and
  ngrok units (`journalctl -u legal-agent`, `journalctl -u legal-ngrok`).
- **ngrok status** — query the local agent API on the VPS
  (`curl -fsS http://127.0.0.1:4040/api/tunnels`) or use
  `legal_deploy.ngrok.discover_public_url`. No running tunnel means no MCP URL.
- **OAuth metadata** — fetch the discovery documents (reachable without a
  bearer token):
  - `https://<ngrok>/.well-known/oauth-protected-resource`
  - `https://<ngrok>/.well-known/oauth-authorization-server`
- **401 challenge** — a `GET`/`POST` to `/mcp` without a valid bearer token must
  return `401` with a `WWW-Authenticate` header pointing at the OAuth server.
  This is expected, not a failure.
- **`api_health` not reachable** — check `/healthz` on the app port over SSH and
  confirm `uv sync` and the systemd services succeeded.
- **Login rejected** — confirm the email is in `LEGAL_MCP_ALLOWED_EMAILS` and
  that `LEGAL_MCP_OAUTH_SIGNING_KEY` / `LEGAL_MCP_OAUTH_LOGIN_SECRET` are set
  (the server fails closed without them).

---

## 11. Cleanup / destroy

Tear down the recorded VPS and clear local state:

```bash
# Inspect the destroy plan first
python -m legal_deploy.deploy destroy --dry-run --json

# Destroy the recorded instance
python -m legal_deploy.deploy destroy --json
```

To destroy a specific instance by id directly through the Cloudzy CLI:

```bash
python -m legal_deploy.cloudzy_cli destroy <instance_id> --dry-run
python -m legal_deploy.cloudzy_cli destroy <instance_id>
```

After a destroy, the deploy state file is cleared, so the next deploy provisions
a fresh instance. Remember to remove the connector from Claude Cowork once the
endpoint is gone.

---

## 12. Last validated deployment

A real end-to-end deployment was validated against a live Cloudzy VPS + ngrok
tunnel and a Codex CLI MCP smoke. The ngrok URL below is a temporary, public
free-tier tunnel (it rotates whenever the ngrok service restarts) — it is safe
to record and carries no secret.

- **MCP URL:** `https://jubilant-willpower-unthawed.ngrok-free.dev/mcp`
- **VPS:** Cloudzy `US-Las-Vegas`, Ubuntu Server 24.04 LTS, 4 GB / 2 vCPU plan.
- **Surfaces verified:** `/healthz` → 200; both OAuth metadata documents
  (`/.well-known/oauth-protected-resource` with `resource` ending in `/mcp`, and
  `/.well-known/oauth-authorization-server` with issuer/authorize/token/register
  endpoints derived from the tunnel base); unauthenticated `/mcp` → `401` with a
  RFC 9728 `WWW-Authenticate: Bearer` challenge; authenticated `/mcp` MCP
  `initialize` → `200`.
- **Codex CLI smoke:** passed — `codex` listed all 8 MCP tools, called
  `legal_sources` (returned all 18 wired source ids), and ran a small
  `legal_search` that completed with `ok: true`.

### Minting a bearer token / running the smoke

The single-user smoke does not need a browser OAuth flow — it mints a signed JWT
bearer token directly from the deployed server's signing config. The deploy
writes that config (signing key, allowed email, issuer = tunnel base, resource =
`<tunnel>/mcp`) into the local, `0600` deploy state file
(`~/.config/legal-agent/deploy-state.json`).

`legal_deploy.smoke_codex` **auto-mints** a token when none is supplied, so the
smoke just needs the URL:

```bash
# Auto-mints from the deploy state file (no token needed):
uv run python -m legal_deploy.smoke_codex \
  --server-url https://<tunnel-host>/mcp

# Or set the remote URL via env and run with no flags:
export LEGAL_MCP_REMOTE_URL=https://<tunnel-host>/mcp
uv run python -m legal_deploy.smoke_codex --server-url "$LEGAL_MCP_REMOTE_URL"
```

Token resolution order in `smoke_codex`:

1. `--bearer-token` flag, then `LEGAL_MCP_BEARER_TOKEN` env var.
2. Auto-mint from `LEGAL_MCP_OAUTH_SIGNING_KEY` + `LEGAL_MCP_ALLOWED_EMAILS`
   env vars (issuer/resource derived from `--server-url` or
   `LEGAL_MCP_OAUTH_ISSUER`).
3. Auto-mint from the deploy state file's recorded signing config.

Pass `--no-auto-mint` to force the anonymous path. The minted token is passed to
`codex` via the `LEGAL_MCP_BEARER_TOKEN` env var (referenced from a temporary
`CODEX_HOME/config.toml`); the raw token is never written to disk or printed.
