---
name: legal-mcp-deployment
description: >-
  Deploy and destroy the full legal API + MCP server on a Cloudzy VPS by
  wrapping `python -m legal_deploy.deploy`, then produce the public MCP URL for
  a Claude Cowork connector. Use when an agent must stand up the end-to-end
  legal MCP service, run its smoke test, or tear it down. Always dry-run first;
  never print or commit raw secret values.
---

# Legal MCP Deployment Skill

This skill teaches an agent how to deploy the **full legal API + MCP server**
end-to-end onto a Cloudzy VPS and hand back a public **MCP URL** that a Claude
Cowork connector can attach to. It is a thin operational wrapper over the
project's deploy orchestrator:

```bash
python -m legal_deploy.deploy <deploy|destroy> [flags] [--dry-run] [--json]
```

Each invocation prints exactly one JSON document to stdout when `--json` is
passed (a normalized error envelope on failure, with a non-zero exit code). No
secret value is ever printed by the command, and you must never echo one
yourself.

This skill owns the **project-specific application deploy** (repo sync, env
file, bootstrap, `uv sync`, systemd services, health check, ngrok tunnel
discovery, MCP URL). It does **not** own raw Cloudzy VPS lifecycle catalog
operations — for picking regions/products/OS images and for low-level
provision/destroy of a bare VPS, use the sibling **cloudzy-deployment** skill
(`agent_skills/cloudzy-deployment/SKILL.md`). The deploy orchestrator here calls
into that same Cloudzy layer for you, so in the normal path you only run the two
commands below.

## When to use this skill

- Deploy the complete legal API + MCP server to a fresh or recorded VPS.
- Produce the Claude Cowork connector URL (the **MCP URL** ending in `/mcp`).
- Smoke-test the deployed MCP endpoint before handing it off.
- Destroy the deployment and stop billing when it is no longer needed.

## Required inputs

The normal deploy needs no positional inputs — it reads selectors from flags
with safe defaults and resolves secrets from files (below). Common overrides:

| Flag | Meaning | Default |
| --- | --- | --- |
| `--region` | Cloudzy region id | `us-east` |
| `--product` | Cloudzy product/plan id | `vps-1` |
| `--os` | OS image id | provider default |
| `--hostname` | Instance hostname | `legal-agent` |
| `--ssh-key` | Cloudzy SSH key id to attach (repeatable) | none |
| `--ssh-key-file` | Local private key used to connect over SSH | agent default |
| `--fresh` | Ignore recorded state and provision a new instance | off |
| `--app-port` | Port the ASGI app binds to | from bootstrap |

Run `python -m legal_deploy.deploy deploy --help` to see the full flag set.

## Required secret files

Secrets are **never committed** and **never passed on the command line**. The
orchestrator resolves them from two gitignored files (env vars override file
values):

| Path | Provides |
| --- | --- |
| `/home/spider/.config/legal-agent/deploy.env` | `CLOUDZY_API_TOKEN` (Cloudzy API token) and any extra `LEGAL_*` deploy secrets such as `LEGAL_CAPSOLVER_API_KEY` written into the remote env file. |
| `~/.config/ngrok/ngrok.yml` | The ngrok `authtoken` used to bring up the public tunnel that exposes the MCP URL. |

Precedence: a value in the environment (`CLOUDZY_API_TOKEN`, `NGROK_AUTHTOKEN`)
wins over the file; the files are the fallback. Confirm both secret sources are
present before a real (non-dry-run) deploy. If either is missing, the command
fails with a `missing_secret` error envelope — resolve the secret rather than
hard-coding it.

### Secret handling rules (NEVER print secrets)

- NEVER paste a raw token value into a command line, a log, a commit, a PR
  description, or a chat message. The deploy `secrets` diagnostics only return
  redacted previews — do not de-redact them.
- Prefer the secret **files** (or env vars) over any flag so values never appear
  in shell history or process listings.
- Do not delete or rewrite the secret files during cleanup; they hold reusable
  credentials.

## DRY-RUN FIRST

`--dry-run` is the critical safe mode. With `--dry-run` (and `--json`) the
command contacts no network, opens no SSH, requires no token, and prints exactly
one JSON document describing the ordered plan, target `app_dir`/`service_user`,
and the rendered command summary, then exits 0.

ALWAYS dry-run a state-changing command first, inspect the printed plan, and
only then re-run without `--dry-run`.

```bash
# Inspect the deploy plan without touching Cloudzy / SSH / ngrok or a token.
python -m legal_deploy.deploy deploy --dry-run --json

# Inspect the destroy plan.
python -m legal_deploy.deploy destroy --dry-run --json
```

## Deploy command

After a clean dry-run, run the real deploy:

```bash
python -m legal_deploy.deploy deploy --json
# add --fresh to ignore recorded state and provision a brand-new VPS
```

The orchestrator provisions-or-reuses a Cloudzy VPS, waits for SSH, syncs the
repo, writes the remote env file (chmod 600), runs the bootstrap script, runs
`uv sync`, starts the systemd services, configures the ngrok authtoken, verifies
API health at `/healthz`, discovers the ngrok public URL, derives the MCP URL,
and records state under `~/.config/legal-agent/deploy-state.json`.

## Final output format

A successful deploy emits one JSON envelope. The fields that matter for the
hand-off:

```json
{
  "ok": true,
  "command": "deploy",
  "instance_id": "<id>",
  "ip": "<vps-ip>",
  "ngrok_url": "https://<subdomain>.ngrok-free.app",
  "mcp_url": "https://<subdomain>.ngrok-free.app/mcp",
  "api_health": { "reachable": true, "body": "..." },
  "next_steps": ["..."]
}
```

The connector value to hand to **Claude Cowork** is `mcp_url` — the ngrok
public URL with the **`/mcp`** suffix. If the MCP endpoint negotiates an
**OAuth** authorization step on first connect, complete that flow in the Cowork
connector UI using the deployed server's advertised authorization endpoint; the
deploy itself does not print any OAuth secret. If `mcp_url` / `ngrok_url` is
`null`, the tunnel was not up yet — re-run the deploy or inspect the
`legal-ngrok` service on the VPS, then retry.

## Smoke test

Verify the live MCP endpoint before handing off the connector URL. Use the
Codex CLI (or any MCP client) to connect to the deployed **MCP URL** and list
its tools:

```bash
# Point an MCP client at the deployed MCP URL (value of mcp_url above).
codex mcp --url "$MCP_URL" list-tools
```

Full smoke automation is implemented separately (deploy step 34); reference and
prefer that automated smoke runner when available. On a smoke failure:

1. Do NOT immediately re-deploy blindly. First inspect the service logs on the
   VPS (`journalctl -u legal-agent`, `journalctl -u legal-ngrok`) and confirm
   `api_health.reachable` and a non-null `mcp_url` in the deploy output.
2. Resolve the root cause (tunnel not up, OAuth misconfig, app not healthy).
3. Retry the smoke test, and only re-run the deploy if the service state on the
   VPS is genuinely wrong.

Treat error envelopes with `"retryable": true` (e.g. `timeout`, `deploy_error`)
as transient and retry after inspecting logs; treat `"retryable": false` (e.g.
`cloudzy_error`, `missing_secret`, `usage_error`) as something to fix in the
request or secrets.

## Destroy command

When the deployment is no longer needed, tear it down to stop billing. This acts
on the recorded instance from local state:

```bash
# 1. Dry-run the destroy and inspect the plan.
python -m legal_deploy.deploy destroy --dry-run --json

# 2. Destroy for real once confirmed.
python -m legal_deploy.deploy destroy --json
```

Destroy removes the recorded Cloudzy instance and clears local deployment state.
It does NOT touch the secret files. After destroy, the recorded `mcp_url` is
dead — remove it from any working notes and from the Claude Cowork connector.

## Installing this skill locally

This skill package lives in the repo under
`agent_skills/legal-mcp-deployment/`. To make it discoverable in the local
Codex skills directory, run the optional installer:

```bash
agent_skills/legal-mcp-deployment/install.sh
```
