---
name: cloudzy-deployment
description: >-
  Provision and destroy Cloudzy VPS instances for the legal pipeline deploy
  tooling by wrapping the `python -m legal_deploy.cloudzy_cli` command. Use when
  an agent must stand up, inspect, or tear down a Cloudzy VPS for hosting the
  legal API/MCP server. Always run dry-run first; never print or commit raw
  token values.
---

# Cloudzy Deployment Skill

This skill teaches an agent how to provision, inspect, and destroy Cloudzy VPS
instances for this project. It is a thin operational wrapper over the project's
deploy CLI:

```bash
python -m legal_deploy.cloudzy_cli <subcommand> [flags] [--dry-run]
```

Every subcommand prints exactly one JSON document to stdout (a normalized error
envelope on failure, with a non-zero exit code). The Cloudzy API token is never
printed by the CLI, and you must never echo it yourself.

## When to use this skill

Use it when the task is to manage Cloudzy infrastructure for the legal
pipeline, specifically:

- Discover what regions, products/plans, OS images, and SSH keys are available
  before creating a VPS.
- List existing instances to find the right one to act on.
- Provision a new VPS to host the legal API/MCP server.
- Poll a freshly created instance until it is ready (readiness polling).
- Destroy a VPS and clean up when it is no longer needed.

Do NOT use this skill for application-level deploy steps (repo sync, `uv sync`,
bootstrap, ngrok). Those belong to the separate deploy-orchestrator skill. This
skill only owns the Cloudzy VPS lifecycle.

## Required environment variables and secret conventions

| Name | Purpose |
| --- | --- |
| `CLOUDZY_API_TOKEN` | Cloudzy API token. Sent by the client as the `API-Token` request header. Resolved from the environment (or `--token`); never printed. |

Deploy secrets follow the project's existing convention. They live in a
gitignored env file that is **never committed**:

```
/home/spider/.config/legal-agent/deploy.env
```

Load `CLOUDZY_API_TOKEN` (and any other deploy secrets) from that file before
running non-dry-run commands. For example, source it into the current shell:

```bash
set -a
. /home/spider/.config/legal-agent/deploy.env
set +a
```

Rules for secrets:

- Never paste a raw token value into a command line, a log, a commit, a PR
  description, or a chat message.
- Prefer the `CLOUDZY_API_TOKEN` environment variable over the `--token` flag so
  the value never appears in shell history or process listings.
- If the token is missing, the CLI errors out cleanly; resolve the secret rather
  than hard-coding it.

## Safety checks (do these first)

1. Confirm the operation. Provisioning and destruction change real, billed
   infrastructure. Re-read the request and make sure the region, product, and
   instance id are exactly what was asked.
2. Verify the secret is loaded for non-dry-run commands:
   `test -n "$CLOUDZY_API_TOKEN"`.
3. For destruction, list instances first and confirm the target instance id
   exists and is the intended one before destroying it.
4. Never run a destroy against an instance id you did not just confirm via
   `instances`.

## DRY-RUN FIRST behavior

`--dry-run` is a global flag accepted by every subcommand. In dry-run mode the
CLI does NOT contact the network and does NOT require a token: it prints a JSON
plan of the action that *would* run and exits 0.

ALWAYS run any state-changing command (`provision`, `destroy`) with `--dry-run`
first, inspect the printed plan, and only then re-run without `--dry-run`.

```bash
# Inspect the provision plan without touching Cloudzy or needing a token.
python -m legal_deploy.cloudzy_cli provision \
  --region <region-id> --product <product-id> --os <os-id> \
  --hostname legal-mcp --label legal-mcp --wait --dry-run

# Inspect the destroy plan.
python -m legal_deploy.cloudzy_cli destroy <instance-id> --dry-run
```

## Discovery (read-only)

These commands list catalog data and existing instances. They need a valid
token unless run with `--dry-run`.

```bash
python -m legal_deploy.cloudzy_cli regions
python -m legal_deploy.cloudzy_cli products
python -m legal_deploy.cloudzy_cli os
python -m legal_deploy.cloudzy_cli ssh-keys
python -m legal_deploy.cloudzy_cli instances
```

Use the ids returned here (region, product, os, ssh-key) as inputs to
`provision`.

## Provisioning

Create a new instance. `--region` and `--product` are required; `--os`,
`--application`, `--hostname`, `--ssh-key` (repeatable), and `--label` are
optional. Pass `--wait` to poll the new instance until it is ready before the
command returns.

```bash
# Dry-run first (see above), then the real run:
python -m legal_deploy.cloudzy_cli provision \
  --region <region-id> --product <product-id> --os <os-id> \
  --ssh-key <ssh-key-id> --hostname legal-mcp --label legal-mcp --wait
```

The JSON result includes the created `instance` object. Record its `id` — you
need it for `wait` and `destroy`.

## Polling readiness

If you did not pass `--wait` to `provision`, poll separately. `wait` blocks
until the instance is ready or the timeout elapses.

```bash
python -m legal_deploy.cloudzy_cli wait <instance-id> --timeout 600 --interval 10
```

`--timeout` (default 600s) and `--interval` (default 10s) control the poll. A
timeout produces a retryable error envelope; re-run `wait` if the instance is
still booting.

## Destruction and cleanup

When the VPS is no longer needed, destroy it to stop billing.

```bash
# 1. Confirm the target.
python -m legal_deploy.cloudzy_cli instances

# 2. Dry-run the destroy and inspect the plan.
python -m legal_deploy.cloudzy_cli destroy <instance-id> --dry-run

# 3. Destroy for real once confirmed.
python -m legal_deploy.cloudzy_cli destroy <instance-id>
```

After destruction, re-run `instances` to confirm the instance is gone. Remove
any local references to the destroyed instance id from working notes. Do not
delete or rewrite `/home/spider/.config/legal-agent/deploy.env` as part of
cleanup — that file holds reusable secrets.

## Reading results

- Success: `{"ok": true, "command": ..., ...}` and exit 0.
- Failure: `{"ok": false, "command": ..., "error": {"code", "message",
  "retryable", "details"}}` and exit 1 (usage errors exit 2).
- Treat `retryable: true` errors (e.g. `timeout`) as transient and retry; treat
  `retryable: false` (e.g. `cloudzy_error`, `usage_error`) as something to fix
  in the request.

## Installing this skill locally

This skill package lives in the repo under
`agent_skills/cloudzy-deployment/`. To make it available to the local Codex
skills directory, run the optional installer:

```bash
agent_skills/cloudzy-deployment/install.sh
```
