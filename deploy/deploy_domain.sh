#!/usr/bin/env bash
#
# Reproducible production deploy for the legal API + MCP server behind a fixed
# domain with automatic HTTPS (Caddy + Let's Encrypt).
#
# This is the always-on counterpart to the ngrok-based orchestrator
# (`python -m deploy.deploy`). It deploys the combined ASGI app
# (`api.main:app`) to an existing SSH-reachable VPS and fronts it with Caddy on
# a stable domain (default: mcp.arglegal.live). The result is a stable MCP URL
# `https://<domain>/mcp` you paste into a Claude Cowork connector.
#
# What it does (every step is idempotent and safe to re-run):
#   1. rsync this repo into APP_DIR on the host (excludes .git/.venv/.work/vendor).
#   2. Render the remote .env (chmod 600) from fixed config + secrets sourced
#      from a LOCAL gitignored deploy env file. Secrets are never printed.
#   3. Remotely install system deps, uv, Caddy, the service user, run `uv sync`,
#      and best-effort vendor BotBrowser.
#   4. Install the `legal-api` systemd unit (uvicorn api.main:app :APP_PORT) and
#      a Caddyfile reverse-proxying the domain -> 127.0.0.1:APP_PORT.
#   5. Reload services and health-check the public domain.
#
# DNS is a prerequisite this script does NOT manage: the domain's A record must
# already point at the host (managed locally via the `namecheap-domains` skill).
# The script warns if DNS does not resolve to the host, because Caddy cannot
# obtain a certificate until it does.
#
# Usage:
#   deploy/deploy_domain.sh --host <ip> [--domain mcp.arglegal.live] [opts]
#   deploy/deploy_domain.sh --dry-run            # render plan, no SSH
#
# Secrets: sourced from a local KEY=VALUE file (default
# ~/.config/legal-agent/deploy.env, chmod 600). See deploy.env.example. Required
# keys: LEGAL_ANYIP_USER, LEGAL_ANYIP_PASS, LEGAL_CAPSOLVER_API_KEY,
# LEGAL_MCP_OAUTH_SIGNING_KEY, LEGAL_MCP_OAUTH_LOGIN_SECRET, LEGAL_API_KEY.
# Optional: LEGAL_FLOXY_USER, LEGAL_FLOXY_PASS, CLOUDZY_API_TOKEN.
#
set -euo pipefail

# ----------------------------------------------------------------------------
# Defaults (override via flags or environment)
# ----------------------------------------------------------------------------
DOMAIN="${DOMAIN:-mcp.arglegal.live}"
HOST="${HOST:-}"
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-}"
APP_DIR="${APP_DIR:-/opt/legal-agent}"
SERVICE_USER="${SERVICE_USER:-legal}"
APP_PORT="${APP_PORT:-8080}"
ALLOWED_EMAIL="${ALLOWED_EMAIL:-yoli@arglegal.live}"
DEPLOY_ENV="${DEPLOY_ENV:-$HOME/.config/legal-agent/deploy.env}"
STATE_FILE="${LEGAL_DEPLOY_STATE_FILE:-$HOME/.config/legal-agent/deploy-state.json}"
DRY_RUN=0
SKIP_SYNC=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '[deploy] %s\n' "$*" >&2; }
die()  { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ----------------------------------------------------------------------------
# Parse args
# ----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --host)          HOST="$2"; shift 2;;
    --domain)        DOMAIN="$2"; shift 2;;
    --ssh-user)      SSH_USER="$2"; shift 2;;
    --ssh-key)       SSH_KEY="$2"; shift 2;;
    --app-dir)       APP_DIR="$2"; shift 2;;
    --service-user)  SERVICE_USER="$2"; shift 2;;
    --app-port)      APP_PORT="$2"; shift 2;;
    --allowed-email) ALLOWED_EMAIL="$2"; shift 2;;
    --deploy-env)    DEPLOY_ENV="$2"; shift 2;;
    --skip-sync)     SKIP_SYNC=1; shift;;
    --dry-run)       DRY_RUN=1; shift;;
    -h|--help)       usage;;
    *) die "unknown argument: $1 (try --help)";;
  esac
done

# Fall back to the recorded deploy-state IP when --host is omitted.
if [ -z "$HOST" ] && [ -f "$STATE_FILE" ]; then
  HOST="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ip",""))' "$STATE_FILE" 2>/dev/null || true)"
  [ -n "$HOST" ] && log "using host $HOST from $STATE_FILE"
fi
[ -n "$HOST" ] || die "no target host: pass --host <ip> (or record one in $STATE_FILE)"

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
[ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")
ssh_run() { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${HOST}" "$@"; }

# The connector / OAuth-resource URL is the bare domain root (no redundant /mcp
# on an already-"mcp." host). Caddy presents the transport at the root and the
# app still mounts it internally at /mcp.
PUBLIC_URL="https://${DOMAIN}"
ISSUER="https://${DOMAIN}"
REMOTE_ENV_FILE="${APP_DIR}/.env"

# ----------------------------------------------------------------------------
# Render artifacts (pure strings; used by both dry-run and the real deploy)
# ----------------------------------------------------------------------------
render_caddyfile() {
  # Present the MCP transport at the domain root so the connector URL is just
  # https://<domain> (no redundant /mcp). The app mounts MCP at /mcp, so rewrite
  # the bare root request onto /mcp/; every other path (OAuth discovery, /oauth,
  # /healthz, /icon.png, /v1, and the legacy /mcp endpoint) passes through.
  cat <<EOF
${DOMAIN} {
	@root path /
	rewrite @root /mcp/
	reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF
}

render_unit() {
  cat <<EOF
[Unit]
Description=Legal API + MCP server (combined ASGI app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${REMOTE_ENV_FILE}
ExecStart=/usr/local/bin/uv run uvicorn api.main:app --host 0.0.0.0 --port ${APP_PORT}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

# ----------------------------------------------------------------------------
# Dry run: render the plan and artifacts, contact nothing.
# ----------------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  cat >&2 <<EOF
[deploy] DRY RUN — no SSH, no secrets read.
  host:          ${HOST}
  domain:        ${DOMAIN}
  public_url:    ${PUBLIC_URL}
  issuer:        ${ISSUER}
  allowed_email: ${ALLOWED_EMAIL}
  app_dir:       ${APP_DIR} (user ${SERVICE_USER}, port ${APP_PORT})
  deploy_env:    ${DEPLOY_ENV}

Ordered steps:
  1. rsync repo -> ${SSH_USER}@${HOST}:${APP_DIR}
  2. render + scp ${REMOTE_ENV_FILE} (chmod 600)
  3. remote bootstrap: apt deps, uv, caddy, service user, uv sync, vendor browser
  4. install systemd unit legal-api.service + /etc/caddy/Caddyfile
  5. reload services; health-check https://${DOMAIN}/{healthz,mcp,icon.png}

--- /etc/caddy/Caddyfile ---
$(render_caddyfile)
--- /etc/systemd/system/legal-api.service ---
$(render_unit)
--- ${REMOTE_ENV_FILE} (keys only) ---
LEGAL_MCP_AUTH_ENABLED, LEGAL_MCP_PUBLIC_URL, LEGAL_MCP_OAUTH_ISSUER,
LEGAL_MCP_ALLOWED_EMAILS, LEGAL_MCP_OAUTH_SIGNING_KEY, LEGAL_MCP_OAUTH_LOGIN_SECRET,
LEGAL_API_KEY, LEGAL_PROXY_ENABLED, LEGAL_PROXY_PROVIDER, LEGAL_PROXY_COUNTRY,
LEGAL_ANYIP_USER, LEGAL_ANYIP_PASS, LEGAL_CAPSOLVER_API_KEY,
[optional] LEGAL_FLOXY_USER, LEGAL_FLOXY_PASS, CLOUDZY_API_TOKEN
EOF
  exit 0
fi

# ----------------------------------------------------------------------------
# Load secrets from the local deploy env file (never printed).
# ----------------------------------------------------------------------------
[ -f "$DEPLOY_ENV" ] || die "deploy env file not found: $DEPLOY_ENV (see deploy/deploy.env.example)"
perms="$(stat -c '%a' "$DEPLOY_ENV" 2>/dev/null || echo '')"
[ "$perms" = "600" ] || log "WARNING: $DEPLOY_ENV is mode ${perms:-?}; recommend chmod 600"
set -a
# shellcheck disable=SC1090
. "$DEPLOY_ENV"
set +a

require_secret() { [ -n "${!1:-}" ] || die "missing required secret '$1' in $DEPLOY_ENV"; }
for k in LEGAL_ANYIP_USER LEGAL_ANYIP_PASS LEGAL_CAPSOLVER_API_KEY \
         LEGAL_MCP_OAUTH_SIGNING_KEY LEGAL_MCP_OAUTH_LOGIN_SECRET LEGAL_API_KEY; do
  require_secret "$k"
done

# ----------------------------------------------------------------------------
# DNS pre-flight (warn only — Caddy needs the A record to issue a certificate).
# ----------------------------------------------------------------------------
resolved="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -n1 || true)"
if [ "$resolved" != "$HOST" ]; then
  log "WARNING: ${DOMAIN} resolves to '${resolved:-<none>}', expected ${HOST}."
  log "         Point the A record at ${HOST} (namecheap-domains skill) or Caddy TLS will fail."
fi

# ----------------------------------------------------------------------------
# 1. Sync the repo.
# ----------------------------------------------------------------------------
if [ "$SKIP_SYNC" -eq 0 ]; then
  log "syncing repo -> ${SSH_USER}@${HOST}:${APP_DIR}"
  ssh_run "mkdir -p ${APP_DIR}"
  rsync_ssh="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
  [ -n "$SSH_KEY" ] && rsync_ssh="$rsync_ssh -i $SSH_KEY"
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '.work' \
    --exclude 'legal/vendor' --exclude '__pycache__' \
    -e "$rsync_ssh" \
    "${REPO_ROOT}/" "${SSH_USER}@${HOST}:${APP_DIR}/"
else
  log "skipping repo sync (--skip-sync)"
fi

# ----------------------------------------------------------------------------
# 2. Render + ship the remote env file (built locally @ 0600, scp'd, locked down).
# ----------------------------------------------------------------------------
log "writing remote env file ${REMOTE_ENV_FILE}"
tmp_env="$(umask 077; mktemp)"
trap 'rm -f "$tmp_env"' EXIT
{
  echo "# Rendered by deploy/deploy_domain.sh — do not commit. chmod 600."
  echo "LEGAL_MCP_AUTH_ENABLED=true"
  echo "LEGAL_MCP_PUBLIC_URL=${PUBLIC_URL}"
  echo "LEGAL_MCP_OAUTH_ISSUER=${ISSUER}"
  echo "LEGAL_MCP_ALLOWED_EMAILS=${ALLOWED_EMAIL}"
  echo "LEGAL_MCP_OAUTH_SIGNING_KEY=${LEGAL_MCP_OAUTH_SIGNING_KEY}"
  echo "LEGAL_MCP_OAUTH_LOGIN_SECRET=${LEGAL_MCP_OAUTH_LOGIN_SECRET}"
  echo "LEGAL_API_KEY=${LEGAL_API_KEY}"
  echo "LEGAL_PROXY_ENABLED=true"
  echo "LEGAL_PROXY_PROVIDER=anyip"
  echo "LEGAL_PROXY_COUNTRY=ar"
  echo "LEGAL_ANYIP_USER=${LEGAL_ANYIP_USER}"
  echo "LEGAL_ANYIP_PASS=${LEGAL_ANYIP_PASS}"
  echo "LEGAL_CAPSOLVER_API_KEY=${LEGAL_CAPSOLVER_API_KEY}"
  [ -n "${LEGAL_FLOXY_USER:-}" ] && echo "LEGAL_FLOXY_USER=${LEGAL_FLOXY_USER}"
  [ -n "${LEGAL_FLOXY_PASS:-}" ] && echo "LEGAL_FLOXY_PASS=${LEGAL_FLOXY_PASS}"
  [ -n "${CLOUDZY_API_TOKEN:-}" ] && echo "CLOUDZY_API_TOKEN=${CLOUDZY_API_TOKEN}"
} > "$tmp_env"
scp "${SSH_OPTS[@]}" "$tmp_env" "${SSH_USER}@${HOST}:${REMOTE_ENV_FILE}" >/dev/null
ssh_run "chown ${SERVICE_USER}:${SERVICE_USER} ${REMOTE_ENV_FILE} 2>/dev/null || true; chmod 600 ${REMOTE_ENV_FILE}"
rm -f "$tmp_env"; trap - EXIT

# ----------------------------------------------------------------------------
# 3-4. Remote bootstrap: deps, uv, caddy, service, units, reverse proxy.
# ----------------------------------------------------------------------------
log "running remote bootstrap (deps, uv, caddy, systemd, caddyfile)"
caddyfile_content="$(render_caddyfile)"
unit_content="$(render_unit)"

ssh_run "APP_DIR='${APP_DIR}' SERVICE_USER='${SERVICE_USER}' APP_PORT='${APP_PORT}' \
         DOMAIN='${DOMAIN}' bash -s" <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[remote] installing system + browser packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg git build-essential pkg-config xvfb fonts-liberation \
  debian-keyring debian-archive-keyring apt-transport-https
# Headless-browser deps are best-effort (names shift across Ubuntu releases).
for pkg in libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
           libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
           libxrandr2 libgbm1 libasound2t64 libatspi2.0-0t64 libpango-1.0-0 libcairo2; do
  apt-get install -y --no-install-recommends "\$pkg" \
    || echo "[remote] browser dep \$pkg unavailable on this release; skipping"
done

echo "[remote] installing uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  [ -x "\$HOME/.local/bin/uv" ] && install -m 0755 "\$HOME/.local/bin/uv" /usr/local/bin/uv || true
fi
command -v uv

echo "[remote] installing caddy"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
command -v caddy

echo "[remote] ensuring service user \$SERVICE_USER and \$APP_DIR"
id -u "\$SERVICE_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "\$SERVICE_USER"
mkdir -p "\$APP_DIR"
chown -R "\$SERVICE_USER":"\$SERVICE_USER" "\$APP_DIR"
[ -f "\$APP_DIR/.env" ] && { chown "\$SERVICE_USER":"\$SERVICE_USER" "\$APP_DIR/.env"; chmod 600 "\$APP_DIR/.env"; }

echo "[remote] uv sync"
SVC_HOME="\$(getent passwd "\$SERVICE_USER" | cut -d: -f6)"
( cd "\$APP_DIR" && sudo -u "\$SERVICE_USER" env HOME="\$SVC_HOME" /usr/local/bin/uv sync )

echo "[remote] vendoring BotBrowser (best-effort)"
if [ -f "\$APP_DIR/legal/scripts/bootstrap.py" ]; then
  ( cd "\$APP_DIR" && sudo -u "\$SERVICE_USER" env HOME="\$SVC_HOME" /usr/local/bin/uv run python legal/scripts/bootstrap.py ) \
    || echo "[remote] BotBrowser vendoring skipped (assets unavailable)"
fi

echo "[remote] installing systemd unit"
cat > /etc/systemd/system/legal-api.service <<'UNIT'
${unit_content}
UNIT

echo "[remote] writing Caddyfile"
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<'CADDY'
${caddyfile_content}
CADDY

echo "[remote] reloading services"
systemctl daemon-reload
systemctl enable --now legal-api.service
systemctl restart legal-api.service
systemctl enable --now caddy
# Reload Caddy config without dropping the listener (falls back to restart).
caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || systemctl restart caddy
echo "[remote] bootstrap done"
REMOTE

# ----------------------------------------------------------------------------
# 5. Health checks against the public domain.
# ----------------------------------------------------------------------------
log "verifying public endpoints (allowing a few seconds for TLS issuance)"
ok=1
sleep 5
check() {
  local path="$1" expect="$2" follow="${3:-}" code
  # ``follow`` (-L) traverses redirects; the MCP transport answers /mcp with a
  # 307 to /mcp/, where the unauthenticated 401 challenge actually lives.
  code="$(curl -s ${follow} -o /dev/null -w '%{http_code}' --max-time 25 "https://${DOMAIN}${path}" || echo 000)"
  if [ "$code" = "$expect" ]; then
    log "  OK   https://${DOMAIN}${path} -> ${code}"
  else
    log "  FAIL https://${DOMAIN}${path} -> ${code} (expected ${expect})"
    ok=0
  fi
}
check "/healthz" "200"
check "/icon.png" "200"
check "/.well-known/oauth-protected-resource" "200"
check "/" "401" "-L"      # root is the MCP endpoint; unauthenticated must challenge
check "/mcp" "401" "-L"   # legacy path still works (after the 307 -> /mcp/)

echo
if [ "$ok" -eq 1 ]; then
  log "DEPLOY OK — MCP URL: ${PUBLIC_URL}"
  log "Paste ${PUBLIC_URL} into a Claude Cowork custom connector."
else
  log "DEPLOY completed with FAILED checks above."
  log "If TLS just started issuing, retry the checks in ~30s, or inspect:"
  log "  ssh ${SSH_USER}@${HOST} 'journalctl -u legal-api -u caddy --no-pager -n 80'"
  exit 1
fi
