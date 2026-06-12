"""Render idempotent VPS bootstrap scripts for the legal API + MCP server.

This module produces shell text (strings) that prepare a fresh Ubuntu-like
Cloudzy VPS to run the combined ASGI app (``api.main:app``) behind **Caddy**,
which fronts a stable domain with automatic HTTPS and presents the MCP
transport at the domain root. Nothing here executes anything or touches real
secrets: it renders scripts, systemd unit text, the Caddyfile, and an env-file
writer that the orchestrator runs remotely after syncing the repo into
``app_dir``.

Design goals:

- **Idempotent.** Every step is guarded so re-running on an existing host is
  safe: ``command -v`` checks before installs, ``id -u`` before ``useradd``,
  ``mkdir -p``, and ``systemctl enable --now``.
- **No embedded secrets.** Env files are written from the deploy-time secret
  loader (see :mod:`deploy.secrets`); this module only renders the
  writer and never bakes a real value into the script.
- **Restrictive perms.** Rendered env files are ``chmod 600`` and owned by the
  service user / root.

This is standalone deploy tooling: it does not import the legal pipeline's
source-access internals. It does, however, reference the real
``legal/scripts/bootstrap.py`` helper (run after repo sync to vendor
BotBrowser profiles), the same script used during local setup.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

#: Default port the combined ASGI app listens on.
DEFAULT_APP_PORT = 8080

#: Path (relative to ``app_dir``) of the existing browser-vendoring helper.
VENDOR_BOOTSTRAP_REL = "legal/scripts/bootstrap.py"

#: Default systemd unit name for the combined ASGI app.
APP_SERVICE_NAME = "legal-api"

#: Default public domain Caddy fronts (overridable per deploy).
DEFAULT_DOMAIN = "mcp.arglegal.live"

#: Core system packages always installed (builds + tooling). These names are
#: stable across recent Ubuntu releases.
SYSTEM_PACKAGES = (
    "ca-certificates",
    "curl",
    "gnupg",
    "git",
    "build-essential",
    "pkg-config",
    "xvfb",
    "fonts-liberation",
    "poppler-utils",  # provides the `pdftotext` binary backing legal.pdf.extract_text
)

#: Headless-browser / Playwright shared-library deps. Ubuntu 24.04 (noble)
#: renamed several of these in the time_t-64 (``t64``) transition
#: (e.g. ``libasound2`` -> ``libasound2t64``). To stay portable across 22.04 and
#: 24.04, these are installed best-effort: each is tried, and a package with no
#: installation candidate on this release is skipped rather than aborting the
#: deploy. The API + MCP server do not need the browser; only the
#: browser-backed legal sources do.
BROWSER_PACKAGES = (
    "libnss3",
    "libnspr4",
    "libatk1.0-0t64",
    "libatk-bridge2.0-0t64",
    "libcups2t64",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libgbm1",
    "libasound2t64",
    "libatspi2.0-0t64",
    "libpango-1.0-0",
    "libcairo2",
)


def render_env_file(
    mapping: dict[str, str],
    *,
    path: str,
    owner: str = "root",
    mode: str = "600",
) -> str:
    """Render a shell snippet that writes ``mapping`` to a ``600`` env file.

    The snippet writes one ``KEY=VALUE`` line per item (values shell-quoted),
    then locks the file down with ``chown`` / ``chmod`` so only the owner can
    read it. Values are written verbatim at deploy time from the secret loader;
    no real secret is baked into the returned text by this function unless the
    caller passes one in ``mapping``.

    :param mapping: ``KEY -> VALUE`` pairs to write.
    :param path: Absolute remote path of the env file.
    :param owner: ``user`` or ``user:group`` to own the file.
    :param mode: chmod mode string (default ``600``).
    """
    owner_user = owner.split(":")[0]
    lines = [
        f"# env file written by the legal deploy bootstrap; permissions {mode}",
        # Create the file locked down to the owner from the start. The env file
        # may be written before the service user exists (the bootstrap creates
        # it), so install as root and chown only if the owner user is present.
        f"install -m {shlex.quote(mode)} /dev/null {shlex.quote(path)}",
        f"cat > {shlex.quote(path)} <<'LEGAL_ENV_EOF'",
    ]
    for key, value in mapping.items():
        lines.append(f"{key}={value}")
    lines.append("LEGAL_ENV_EOF")
    lines.append(
        f"id -u {shlex.quote(owner_user)} >/dev/null 2>&1 && "
        f"chown {shlex.quote(owner)} {shlex.quote(path)} || true"
    )
    lines.append(f"chmod {shlex.quote(mode)} {shlex.quote(path)}")
    return "\n".join(lines) + "\n"


def render_caddyfile(domain: str, app_port: int = DEFAULT_APP_PORT) -> str:
    """Render the Caddyfile fronting ``domain`` -> the app on ``app_port``.

    Caddy obtains/renews a Let's Encrypt certificate for ``domain`` and presents
    the MCP transport at the domain ROOT: it rewrites the bare ``/`` request onto
    ``/mcp/`` (the app mounts MCP at ``/mcp``), so the connector URL is just
    ``https://<domain>``. Every other path (OAuth discovery, ``/oauth``,
    ``/healthz``, ``/icon.png``, ``/v1``, and the legacy ``/mcp`` endpoint) is
    reverse-proxied through unchanged.
    """
    if not domain.strip():
        raise ValueError("domain must be a non-empty hostname")
    return (
        f"{domain} {{\n"
        f"\t@root path /\n"
        f"\trewrite @root /mcp/\n"
        f"\treverse_proxy 127.0.0.1:{app_port}\n"
        f"}}\n"
    )


def render_systemd_units(
    *,
    app_dir: str,
    service_user: str,
    app_port: int = DEFAULT_APP_PORT,
    app_env_file: str,
    app_service_name: str = APP_SERVICE_NAME,
) -> dict[str, str]:
    """Return ``{unit_filename: unit_text}`` for the app service.

    The app unit runs ``uv run uvicorn api.main:app`` bound to ``app_port`` from
    ``app_dir`` as ``service_user``, loading the restricted env file. Caddy
    (installed/configured by the bootstrap script, not a rendered unit here)
    fronts it on the public domain. The unit is idempotent to install
    (overwriting the file is safe) and enabled with ``systemctl enable --now``.
    """
    app_unit = f"""[Unit]
Description=Legal API + MCP server (combined ASGI app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={service_user}
WorkingDirectory={app_dir}
EnvironmentFile=-{app_env_file}
ExecStart=/usr/local/bin/uv run uvicorn api.main:app --host 0.0.0.0 --port {app_port}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

    return {f"{app_service_name}.service": app_unit}


def render_bootstrap_script(
    *,
    app_dir: str,
    service_user: str,
    domain: str = DEFAULT_DOMAIN,
    app_port: int = DEFAULT_APP_PORT,
    app_service_name: str = APP_SERVICE_NAME,
    app_env_file: str | None = None,
) -> str:
    """Render the idempotent bootstrap shell script for a fresh VPS.

    The returned script (run remotely as root by the orchestrator) is safe to
    re-run. It:

    1. installs system packages (apt-get) for builds + headless browser deps,
    2. installs ``uv`` (skipped if already present) and Python via uv,
    3. installs ``caddy`` from its apt repository (skipped if present),
    4. creates ``service_user`` and ``app_dir`` (idempotently),
    5. runs ``uv sync`` in ``app_dir`` (repo is synced there beforehand),
    6. runs the existing ``legal/scripts/bootstrap.py`` (via uv) to vendor
       BotBrowser profiles,
    7. installs + enables the app systemd unit and writes/enables the Caddyfile
       fronting ``domain`` with automatic HTTPS.

    :param app_dir: Absolute remote deployment directory holding the repo.
    :param service_user: Unprivileged user that owns/runs the service.
    :param domain: Public domain Caddy fronts (reverse-proxy to the app).
    :param app_port: Port the ASGI app binds to.
    :param app_env_file: Remote env file path loaded by the app unit. Defaults
        to ``<app_dir>/.env`` (chmod 600, owned by ``service_user``).
    """
    if not app_dir.startswith("/"):
        raise ValueError("app_dir must be an absolute path")

    env_file = app_env_file or str(PurePosixPath(app_dir) / ".env")
    vendor_script = str(PurePosixPath(app_dir) / VENDOR_BOOTSTRAP_REL)

    units = render_systemd_units(
        app_dir=app_dir,
        service_user=service_user,
        app_port=app_port,
        app_env_file=env_file,
        app_service_name=app_service_name,
    )
    caddyfile = render_caddyfile(domain, app_port)

    q_app_dir = shlex.quote(app_dir)
    q_user = shlex.quote(service_user)
    q_env_file = shlex.quote(env_file)
    q_vendor_script = shlex.quote(vendor_script)
    packages = " ".join(shlex.quote(p) for p in SYSTEM_PACKAGES)
    browser_packages = " ".join(shlex.quote(p) for p in BROWSER_PACKAGES)

    # Heredocs that install each systemd unit idempotently.
    unit_blocks: list[str] = []
    for filename, text in units.items():
        unit_path = f"/etc/systemd/system/{filename}"
        unit_blocks.append(
            f"cat > {shlex.quote(unit_path)} <<'LEGAL_UNIT_EOF'\n"
            f"{text}LEGAL_UNIT_EOF"
        )
    units_install = "\n".join(unit_blocks)

    enable_blocks = "\n".join(
        f"systemctl enable --now {shlex.quote(name)}" for name in units
    )
    # `enable --now` starts a stopped unit but does NOT restart a running one, so
    # on a redeploy the app would keep its old in-memory env/code. Restart the
    # app unit explicitly so a new env file (e.g. an updated public URL) and
    # freshly synced code take effect every time.
    restart_app = f"systemctl restart {shlex.quote(f'{app_service_name}.service')}"

    return f"""#!/usr/bin/env bash
# Idempotent bootstrap for a fresh Ubuntu-like Cloudzy VPS hosting the legal
# API + MCP server. Safe to re-run. Generated by deploy.bootstrap; do not
# embed real secrets here -- the env file is written from the secret loader.
set -euo pipefail

APP_DIR={q_app_dir}
SERVICE_USER={q_user}
APP_ENV_FILE={q_env_file}
APP_PORT={app_port}

echo "[bootstrap] installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends {packages}

echo "[bootstrap] installing headless-browser deps (best-effort, per package)"
# Names differ across Ubuntu releases (the 24.04 t64 transition), so install
# each browser dep on its own and tolerate any with no installation candidate.
for pkg in {browser_packages}; do
  apt-get install -y --no-install-recommends "$pkg" \\
    || echo "[bootstrap] browser dep '$pkg' unavailable on this release; skipping"
done

echo "[bootstrap] installing uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs into ~/.local/bin or ~/.cargo/bin; expose it system-wide.
  if [ -x "$HOME/.local/bin/uv" ]; then
    install -m 0755 "$HOME/.local/bin/uv" /usr/local/bin/uv
    [ -x "$HOME/.local/bin/uvx" ] && install -m 0755 "$HOME/.local/bin/uvx" /usr/local/bin/uvx || true
  elif [ -x "$HOME/.cargo/bin/uv" ]; then
    install -m 0755 "$HOME/.cargo/bin/uv" /usr/local/bin/uv
  fi
fi
command -v uv

echo "[bootstrap] installing caddy"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \\
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \\
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
command -v caddy

echo "[bootstrap] creating service user $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "[bootstrap] creating deployment directory $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# The orchestrator (step 30) syncs the repo into $APP_DIR before/around this
# point and writes $APP_ENV_FILE from the secret loader. Guard the env file's
# permissions in case it already exists.
if [ -f "$APP_ENV_FILE" ]; then
  chown "$SERVICE_USER":"$SERVICE_USER" "$APP_ENV_FILE"
  chmod 600 "$APP_ENV_FILE"
fi

echo "[bootstrap] syncing Python environment with uv"
if [ -f "$APP_DIR/pyproject.toml" ]; then
  ( cd "$APP_DIR" && sudo -u "$SERVICE_USER" --preserve-env=HOME env HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)" /usr/local/bin/uv sync )
fi

echo "[bootstrap] vendoring BotBrowser profiles via legal/scripts/bootstrap.py"
# BotBrowser assets (and the drone profiles tree) are absent on a clean VPS, so
# this step is best-effort: the browser/captcha sources will be unavailable, but
# the API + MCP server (and the non-browser tools the smoke exercises) run fine.
# Do not let a missing BotBrowser source abort the deploy.
if [ -f {q_vendor_script} ]; then
  ( cd "$APP_DIR" && sudo -u "$SERVICE_USER" --preserve-env=HOME env HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)" /usr/local/bin/uv run python {q_vendor_script} ) \
    || echo "[bootstrap] BotBrowser vendoring skipped (assets unavailable on this host)"
fi

echo "[bootstrap] installing systemd units"
{units_install}

systemctl daemon-reload
{enable_blocks}
{restart_app}

echo "[bootstrap] writing Caddyfile for {domain}"
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<'LEGAL_CADDY_EOF'
{caddyfile}LEGAL_CADDY_EOF
systemctl enable --now caddy
# Reload Caddy config without dropping the listener (falls back to restart).
caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || systemctl restart caddy

echo "[bootstrap] done"
"""
