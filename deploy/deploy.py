"""End-to-end deployment orchestrator for the legal API + MCP server VPS.

Runnable as ``python -m deploy.deploy``. This module composes the existing
deploy building blocks into a single deploy flow:

- :mod:`deploy.cloudzy` (``CloudzyClient``) to provision / reuse / poll /
  destroy a Cloudzy VPS,
- :mod:`deploy.secrets` (``load_deploy_secrets``) to resolve the Cloudzy token
  and runtime secrets without ever printing raw values,
- :mod:`deploy.bootstrap` (``render_bootstrap_script`` /
  ``render_systemd_units`` / ``render_caddyfile`` / ``render_env_file``) to
  render the remote setup,
- :mod:`deploy.domain` for the bare-domain public URL / OAuth env and the
  Namecheap ``--host`` label used to repoint DNS at a fresh VPS.

The deploy flow: provision-or-reuse a VPS, (on ``--fresh``) repoint the
Namecheap DNS A record at the new IP and wait for it to resolve, wait for SSH
(paramiko), sync the repo into ``app_dir``, write the remote env file (chmod
600) with the bare-domain public URL/issuer, run the bootstrap script (installs
Caddy and writes the Caddyfile), ``uv sync``, start the app service, verify
local + public health, and record deployment state locally under
``~/.config/legal-agent/deploy-state.json``. The reported connector URL is the
stable domain ``https://<domain>``. ``destroy`` tears down a recorded instance.

``--dry-run`` is the critical safe mode: with ``--dry-run`` (and ``--json``) the
command contacts no network, requires no token, opens no SSH, runs no DNS
automation, and prints exactly one JSON document describing the ordered plan,
target ``app_dir`` / ``service_user`` / ``domain``, and the rendered command
summary, then exits 0.

This is standalone deploy tooling and does not import the legal pipeline's
source-access internals.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as _secrets_mod
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from deploy.bootstrap import (
    APP_SERVICE_NAME,
    DEFAULT_APP_PORT,
    DEFAULT_DOMAIN,
    render_bootstrap_script,
    render_env_file,
    render_systemd_units,
)
from deploy.cloudzy import (
    CloudzyClient,
    CloudzyError,
    CloudzyTimeoutError,
    CreateInstanceRequest,
    created_instance_id,
)
from deploy.domain import (
    dns_host_label,
    oauth_env_updates_for_domain,
    public_url_for_domain,
    registered_domain,
)
from deploy.secrets import (
    CLOUDZY_TOKEN_KEY,
    DeploySecretError,
    load_deploy_secrets,
)

# --- MCP / OAuth env keys written to the remote env file --------------------

MCP_SIGNING_KEY = "LEGAL_MCP_OAUTH_SIGNING_KEY"
MCP_LOGIN_SECRET = "LEGAL_MCP_OAUTH_LOGIN_SECRET"
MCP_ALLOWED_EMAILS = "LEGAL_MCP_ALLOWED_EMAILS"
MCP_AUTH_ENABLED = "LEGAL_MCP_AUTH_ENABLED"
MCP_TOKEN_TTL = "LEGAL_MCP_OAUTH_TOKEN_TTL_SECONDS"
API_KEY_ENV = "LEGAL_API_KEY"

#: Production runtime defaults seeded into the remote env when not already
#: supplied via the deploy env file (``secrets.extra``). Mirrors the values the
#: legacy ``deploy_domain.sh`` hardcoded: browser-backed sources egress through
#: an Argentine AnyIP proxy, and access tokens last 24h (clients silently renew
#: via the refresh token).
DEFAULT_RUNTIME_ENV = {
    "LEGAL_PROXY_ENABLED": "true",
    "LEGAL_PROXY_PROVIDER": "anyip",
    "LEGAL_PROXY_COUNTRY": "ar",
    MCP_TOKEN_TTL: "86400",
}

#: Default allowed email for the single-user MCP login (the production
#: allow-list; override with --allowed-email).
DEFAULT_ALLOWED_EMAIL = "yoli@arglegal.live"

# -- defaults ----------------------------------------------------------------

#: Remote deployment directory holding the synced repo.
DEFAULT_APP_DIR = "/opt/legal-agent"
#: Unprivileged service user that owns/runs the systemd services.
DEFAULT_SERVICE_USER = "legal"
#: Remote env file loaded by the app systemd unit (chmod 600).
DEFAULT_REMOTE_ENV_FILE = str(PurePosixPath(DEFAULT_APP_DIR) / ".env")

#: Default Cloudzy provisioning selectors (overridable on the CLI). These are
#: validated live IDs from the Cloudzy Developer API: a US-New-York region, a
#: 4 GB / 2 vCPU default plan (enough headroom for uv sync + BotBrowser vendor),
#: and the Ubuntu Server 24.04 LTS image. Plans go out of sales / out of stock
#: over time (PRODUCT_IS_OUT_OF_SALES) — rediscover current in-stock IDs with
#: ``python -m deploy.cloudzy_cli regions`` / ``products`` (products need a
#: regionId; pick one with ``isOutOfSales: false`` and ``remainingActualStock``).
DEFAULT_REGION = "US-New-York"
DEFAULT_PRODUCT = "6a113a85-d6c1-4cf2-97be-93a67538d206"
DEFAULT_OS = "4700a1df2452ce24d8349625ba8b45d4bd1c54e60ad06ff879bce048935d57ff"
DEFAULT_HOSTNAME = "legal-agent"
DEFAULT_SSH_USER = "root"

#: Local deployment state file.
STATE_DIR = Path.home() / ".config" / "legal-agent"
STATE_FILE = STATE_DIR / "deploy-state.json"

#: SSH wait defaults.
DEFAULT_SSH_TIMEOUT = 600.0
DEFAULT_SSH_INTERVAL = 10.0
SSH_PORT = 22


class DeployError(RuntimeError):
    """Raised when the deploy flow cannot proceed."""


# -- state persistence -------------------------------------------------------


def _state_file(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get("LEGAL_DEPLOY_STATE_FILE")
    if override:
        return Path(override)
    return STATE_FILE


def load_state(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load recorded deployment state, or an empty dict when absent."""
    state_path = _state_file(path)
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(
    state: dict[str, Any], path: str | os.PathLike[str] | None = None
) -> Path:
    """Persist deployment state to the local state file (0600)."""
    state_path = _state_file(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        state_path.chmod(0o600)
    except OSError:
        pass
    return state_path


# -- instance metadata extraction -------------------------------------------


def _instance_id(instance: Any) -> str | None:
    if not isinstance(instance, dict):
        return None
    for key in ("id", "instanceId", "instance_id", "uuid"):
        value = instance.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _local_pubkey_body() -> str | None:
    """Return the base64 body of this machine's local SSH public key, if any."""
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        path = ssh_dir / name
        if path.exists():
            parts = path.read_text(encoding="utf-8", errors="ignore").split()
            if len(parts) >= 2:
                return parts[1]
    return None


def _matching_ssh_key_ids(client: Any) -> list[str]:
    """Return registered Cloudzy SSH key ids matching the local public key.

    Lets ``deploy --fresh`` attach the right key without a hardcoded id: the
    new VPS authorizes this machine's key so the deploy can connect over SSH.
    """
    body = _local_pubkey_body()
    if not body:
        return []
    try:
        raw = client.list_ssh_keys()
    except Exception:
        return []
    keys = raw if isinstance(raw, list) else (
        raw.get("data") or raw.get("sshKeys") or raw.get("ssh_keys") or []
        if isinstance(raw, dict)
        else []
    )
    ids: list[str] = []
    for key in keys:
        if isinstance(key, dict) and body in json.dumps(key):
            kid = key.get("id") or key.get("uuid")
            if kid is not None:
                ids.append(str(kid))
    return ids


def _instance_ip(instance: Any) -> str | None:
    if not isinstance(instance, dict):
        return None
    for key in (
        "ip",
        "ipv4",
        "ip_address",
        "ipAddress",
        "mainIp",
        "main_ip",
        "publicIp",
        "public_ip",
    ):
        value = instance.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some APIs nest networking under a list/dict.
    networks = instance.get("networks") or instance.get("network")
    if isinstance(networks, dict):
        return _instance_ip(networks)
    if isinstance(networks, list):
        for entry in networks:
            ip = _instance_ip(entry)
            if ip:
                return ip
    return None


# -- ordered plan ------------------------------------------------------------


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Describe the ordered deploy steps without contacting anything.

    Safe for ``--dry-run``: pure string rendering, no network / SSH / token.
    """
    app_dir = args.app_dir
    service_user = args.service_user
    app_port = args.app_port
    remote_env_file = args.remote_env_file or str(PurePosixPath(app_dir) / ".env")

    units = render_systemd_units(
        app_dir=app_dir,
        service_user=service_user,
        app_port=app_port,
        app_env_file=remote_env_file,
    )

    if args.command == "destroy":
        state = load_state(args.state_file)
        steps = [
            "load recorded deployment state",
            "resolve Cloudzy token from the deploy secret loader",
            "destroy the recorded Cloudzy instance",
            "clear recorded deployment state",
        ]
        return {
            "action": "destroy",
            "steps": steps,
            "instance_id": state.get("instance_id"),
            "state_file": str(_state_file(args.state_file)),
        }

    # deploy plan
    domain = getattr(args, "domain", DEFAULT_DOMAIN)
    public_url = public_url_for_domain(domain)
    fresh = bool(args.fresh)
    dns_repoint = fresh and not getattr(args, "no_dns", False)

    rendered_env_keys = [
        MCP_AUTH_ENABLED,
        "LEGAL_MCP_PUBLIC_URL",
        "LEGAL_MCP_OAUTH_ISSUER",
        CLOUDZY_TOKEN_KEY,
        "LEGAL_CAPSOLVER_API_KEY",
    ]
    remote_commands = [
        f"ssh {args.ssh_user}@<ip> : write remote env file via render_env_file "
        "(chmod 600, bare-domain public URL)",
        f"ssh : run rendered bootstrap.sh (apt deps, uv, caddy, service user, "
        f"uv sync, vendor profiles, app unit, Caddyfile for {domain})",
        f"systemctl enable --now {APP_SERVICE_NAME}.service caddy",
        f"curl http://127.0.0.1:{app_port}/healthz : verify local API health",
        f"curl https://{domain}/healthz : verify public health (TLS)",
    ]

    steps = ["load deploy secrets (Cloudzy token + runtime secrets)"]
    steps.append(
        "reuse recorded instance"
        if (not fresh and load_state(args.state_file).get("instance_id"))
        else "provision a new Cloudzy instance"
    )
    steps.append("poll the instance until it reaches a ready state")
    steps.append(f"wait for SSH on port {SSH_PORT} (paramiko)")
    if dns_repoint:
        steps.append(
            f"repoint Namecheap A record {domain} (host "
            f"'{dns_host_label(domain)}') -> the new IP via nc_browser.py"
        )
        steps.append(f"wait for DNS to resolve {domain} to the new IP")
    steps.append(f"sync the repo into {app_dir} (rsync over ssh)")
    steps.append(f"write remote env file {remote_env_file} (chmod 600)")
    steps.append(f"run the rendered bootstrap script remotely (installs Caddy for {domain})")
    steps.append(f"uv sync in {app_dir}")
    steps.append(f"start systemd service(s) ({', '.join(units)}) + caddy")
    steps.append(f"verify API health at /healthz on port {app_port}")
    steps.append(f"verify public health at https://{domain}/healthz")
    steps.append("record deployment state locally")

    return {
        "action": "deploy",
        "app_dir": app_dir,
        "service_user": service_user,
        "app_port": app_port,
        "domain": domain,
        "public_url": public_url,
        "remote_env_file": remote_env_file,
        "ssh_user": args.ssh_user,
        "region": args.region,
        "product": args.product,
        "hostname": args.hostname,
        "fresh": fresh,
        "dns_repoint": dns_repoint,
        "state_file": str(_state_file(args.state_file)),
        "steps": steps,
        "systemd_units": list(units),
        "remote_env_keys": rendered_env_keys,
        "remote_command_summary": remote_commands,
    }


# -- secret loading (redaction-safe) ----------------------------------------


def _load_secrets(args: argparse.Namespace, *, require: bool) -> Any:
    return load_deploy_secrets(
        deploy_env_file=args.deploy_env_file,
        require=require,
    )


# -- SSH helpers -------------------------------------------------------------


def wait_for_ssh(
    ip: str,
    *,
    port: int = SSH_PORT,
    timeout: float = DEFAULT_SSH_TIMEOUT,
    interval: float = DEFAULT_SSH_INTERVAL,
) -> None:
    """Block until a TCP connection to ``ip:port`` succeeds or times out."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((ip, port), timeout=10.0):
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise DeployError(f"SSH on {ip}:{port} not reachable within {timeout}s")
        time.sleep(interval)


def _ssh_connect(ip: str, *, user: str, key_path: str | None) -> Any:
    import paramiko  # imported lazily so dry-run needs no paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {"hostname": ip, "username": user, "timeout": 30.0}
    if key_path:
        connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
    client.connect(**connect_kwargs)
    return client


def _run_remote(client: Any, command: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def _sync_repo(ip: str, *, user: str, app_dir: str, key_path: str | None) -> None:
    """Sync the local repo into the remote ``app_dir`` via rsync over ssh."""
    repo_root = Path(__file__).resolve().parent.parent
    ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new"
    if key_path:
        ssh_cmd += f" -i {Path(key_path).expanduser()}"
    cmd = [
        "rsync",
        "-az",
        "--delete",
        "--exclude",
        ".git",
        "--exclude",
        ".venv",
        "--exclude",
        ".work",
        "-e",
        ssh_cmd,
        f"{repo_root}/",
        f"{user}@{ip}:{app_dir}/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise DeployError(f"rsync failed: {result.stderr.strip()}")


# -- deploy flow -------------------------------------------------------------


def _build_mcp_env(
    secrets: Any,
    *,
    domain: str,
    allowed_email: str,
    reuse: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the remote env mapping (secrets + MCP/OAuth config + public URL).

    Generates strong OAuth signing/login secrets and a ``LEGAL_API_KEY`` the
    first time, reusing values recorded in ``reuse`` (the prior deploy state) on
    a redeploy so issued tokens stay valid. The public URL / issuer are the
    **bare domain** (Caddy serves the MCP transport at the domain root), set up
    front here — no post-deploy discovery round-trip. Production runtime
    defaults (Argentine AnyIP proxy, 24h token TTL) are seeded only when the
    deploy env file does not already provide them.
    """
    reuse = reuse or {}
    env: dict[str, str] = {}
    if secrets.cloudzy_api_token:
        env[CLOUDZY_TOKEN_KEY] = secrets.cloudzy_api_token
    for key, value in secrets.extra.items():
        if value:
            env[key] = value

    # Seed production runtime defaults without overriding anything the deploy
    # env file explicitly set.
    for key, value in DEFAULT_RUNTIME_ENV.items():
        env.setdefault(key, value)

    env[MCP_AUTH_ENABLED] = "true"
    env[MCP_ALLOWED_EMAILS] = allowed_email
    # Bare-domain public URL + issuer (== resource == connector URL).
    env.update(oauth_env_updates_for_domain(domain))
    # Stable OAuth secrets precedence: an explicit value in the deploy env file
    # (already in ``env`` from ``secrets.extra``) wins, so an owner-chosen login
    # secret / signing key / API key is honored; otherwise reuse the prior
    # deploy's value (so issued tokens stay valid across redeploys); otherwise
    # generate a strong one.
    env[MCP_SIGNING_KEY] = (
        env.get(MCP_SIGNING_KEY) or reuse.get(MCP_SIGNING_KEY) or _secrets_mod.token_urlsafe(48)
    )
    env[MCP_LOGIN_SECRET] = (
        env.get(MCP_LOGIN_SECRET) or reuse.get(MCP_LOGIN_SECRET) or _secrets_mod.token_urlsafe(24)
    )
    env[API_KEY_ENV] = (
        env.get(API_KEY_ENV) or reuse.get(API_KEY_ENV) or _secrets_mod.token_urlsafe(24)
    )
    return env


def run_deploy(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the full deploy flow and return a JSON-safe result envelope."""
    secrets = _load_secrets(args, require=True)

    state = {} if args.fresh else load_state(args.state_file)
    instance_id = state.get("instance_id")

    client = CloudzyClient(token=secrets.cloudzy_api_token)
    try:
        if instance_id:
            instance = client.get_instance(instance_id)
        else:
            # Attach an SSH key so the deploy can log in over key auth. Use the
            # explicit --ssh-key ids, else auto-resolve the registered Cloudzy
            # key(s) matching this machine's local public key. Without a key the
            # VPS only has a (emailed) root password and SSH auth fails.
            ssh_keys = list(args.ssh_key) if args.ssh_key else _matching_ssh_key_ids(client)
            if not ssh_keys:
                raise DeployError(
                    "no SSH key to attach: pass --ssh-key <cloudzy-key-id> or "
                    "register this machine's ~/.ssh/*.pub key in Cloudzy"
                )
            request = CreateInstanceRequest(
                region=args.region,
                product=args.product,
                operating_system=args.os,
                hostname=args.hostname,
                ssh_keys=ssh_keys,
                label=args.label,
            )
            instance = client.create_instance(request)
            # The create response nests created instances under
            # ``data.instances`` (each possibly an envelope), so search deeply
            # for the id rather than expecting a flat shape.
            instance_id = created_instance_id(instance) or _instance_id(instance)
            if instance_id is None:
                raise DeployError("could not determine instance id after provision")

        ready = client.wait_for_instance(
            instance_id, timeout=args.timeout, interval=args.interval
        )
        ip = _instance_ip(ready) or _instance_ip(instance)
        if ip is None:
            raise DeployError("could not determine instance IP address")
    finally:
        client.close()

    wait_for_ssh(ip, timeout=args.ssh_timeout, interval=args.ssh_interval)

    domain = args.domain
    public_url = public_url_for_domain(domain)
    remote_env_file = args.remote_env_file or str(
        PurePosixPath(args.app_dir) / ".env"
    )

    # On a fresh provision the instance has a brand-new IP. Record it early so a
    # later DNS/deploy failure can't orphan the VPS, then repoint the Namecheap
    # A record at it and wait for DNS to resolve (Caddy needs the A record live
    # to issue a Let's Encrypt certificate). Skipped with --no-dns.
    dns: dict[str, Any] | None = None
    if args.fresh:
        save_state(
            {
                "instance_id": instance_id,
                "ip": ip,
                "domain": domain,
                "app_dir": args.app_dir,
                "service_user": args.service_user,
                "ssh_user": args.ssh_user,
            },
            args.state_file,
        )
        if not args.no_dns:
            dns = _repoint_dns(domain, ip)
            dns["resolved"] = _wait_for_dns(
                domain, ip, timeout=args.dns_timeout, interval=args.dns_interval
            )

    allowed_email = getattr(args, "allowed_email", None) or DEFAULT_ALLOWED_EMAIL
    # Build the full remote env: deploy secrets + MCP/OAuth config + the
    # bare-domain public URL/issuer (set up front; Caddy serves at the domain
    # root). Reuse the signing key / login secret / API key from prior state on
    # a redeploy so any already-issued bearer token keeps validating.
    env_mapping = _build_mcp_env(
        secrets, domain=domain, allowed_email=allowed_email, reuse=state
    )
    signing_key = env_mapping[MCP_SIGNING_KEY]

    bootstrap_script = render_bootstrap_script(
        app_dir=args.app_dir,
        service_user=args.service_user,
        domain=domain,
        app_port=args.app_port,
        app_env_file=remote_env_file,
    )
    env_snippet = render_env_file(
        env_mapping,
        path=remote_env_file,
        owner=f"{args.service_user}:{args.service_user}",
    )

    client_ssh = _ssh_connect(ip, user=args.ssh_user, key_path=args.ssh_key_file)
    api_health: dict[str, Any] | None = None
    try:
        _run_remote(client_ssh, f"mkdir -p {args.app_dir}")
        _sync_repo(
            ip,
            user=args.ssh_user,
            app_dir=args.app_dir,
            key_path=args.ssh_key_file,
        )

        # Write env file (chmod 600, bare-domain public URL) then run bootstrap
        # (installs deps incl. uv + Caddy, uv sync, vendors BotBrowser, installs
        # the app unit, writes/enables the Caddyfile fronting the domain).
        for label, script in (
            ("env-file", env_snippet),
            ("bootstrap", bootstrap_script),
        ):
            code, out, err = _run_remote(
                client_ssh,
                f"bash -s <<'LEGAL_DEPLOY_EOF'\n{script}\nLEGAL_DEPLOY_EOF",
            )
            if code != 0:
                raise DeployError(
                    f"remote {label} step failed (exit {code}): {err.strip()[:800]}"
                )

        # Verify local API health (Caddy fronts it on the public domain).
        code, out, _ = _run_remote(
            client_ssh,
            f"curl -fsS http://127.0.0.1:{args.app_port}/healthz || true",
        )
        api_health = {"reachable": code == 0, "body": out.strip()[:500]}
    finally:
        client_ssh.close()

    # Verify the PUBLIC endpoint (through Caddy + TLS), retrying while the
    # certificate issues. A miss here is a soft warning, not a hard failure:
    # Caddy retries ACME on its own and DNS propagation can lag.
    public_health = _check_public_health(domain)

    mcp_url = public_url  # the bare-domain connector URL

    new_state = {
        "instance_id": instance_id,
        "ip": ip,
        "app_dir": args.app_dir,
        "service_user": args.service_user,
        "app_port": args.app_port,
        "domain": domain,
        "remote_env_file": remote_env_file,
        "mcp_url": mcp_url,
        "ssh_user": args.ssh_user,
        "allowed_email": allowed_email,
        # OAuth config needed to mint a bearer token for the smoke. The state
        # file is written chmod 600 (local secret), same posture as deploy.env.
        MCP_SIGNING_KEY: signing_key,
        MCP_ALLOWED_EMAILS: allowed_email,
        MCP_LOGIN_SECRET: env_mapping[MCP_LOGIN_SECRET],
        API_KEY_ENV: env_mapping[API_KEY_ENV],
        "oauth_issuer": public_url,
        "oauth_resource": mcp_url,
    }
    state_path = save_state(new_state, args.state_file)

    return {
        "ok": True,
        "command": "deploy",
        "instance_id": instance_id,
        "ip": ip,
        "domain": domain,
        "mcp_url": mcp_url,
        "api_health": api_health,
        "public_health": public_health,
        "dns": dns,
        "state_file": str(state_path),
        "secrets": secrets.diagnostics(),
        "next_steps": _next_steps(ip, mcp_url, public_health),
    }


# -- Namecheap DNS + public health helpers ----------------------------------


def _repoint_dns(domain: str, ip: str) -> dict[str, Any]:
    """Repoint the Namecheap A record for ``domain`` at ``ip``.

    Shells out LOCALLY to the namecheap-domains skill's browser automation
    (``nc_browser.py dns-set-ip <domain> <ip> --host <label>``), which logs into
    Namecheap with the saved Firefox profile, deletes existing records on the
    host, and writes a single A record (idempotent, self-verifying). Only DNS
    data (domain/host/ip) is handled here — never a secret. Raises
    :class:`DeployError` on failure so the caller can surface the recorded IP for
    a manual repoint + redeploy.
    """
    script = (
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "skills"
        / "namecheap-domains"
        / "scripts"
        / "nc_browser.py"
    )
    if not script.exists():
        raise DeployError(f"namecheap DNS script not found: {script}")
    # The Namecheap DNS UI is managed at the registered (apex) domain with a
    # host label, NOT the full subdomain: arglegal.live + --host mcp.
    apex = registered_domain(domain)
    host = dns_host_label(domain)
    base = ["uv", "run", "python", str(script)]
    # Ensure a logged-in browser session: `start` launches the persistent
    # browser, `login` authenticates it (dns ops fail / see no records without
    # this). Both are best-effort here — a real failure surfaces as a non-ok
    # dns-set-ip below.
    subprocess.run(base + ["start"], capture_output=True, text=True)
    subprocess.run(base + ["login"], capture_output=True, text=True)
    proc = subprocess.run(
        base + ["dns-set-ip", apex, ip, "--host", host],
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "").strip()
    try:
        parsed = json.loads(out) if out else {}
    except ValueError:
        parsed = {}
    if proc.returncode != 0 or not parsed.get("ok", False):
        detail = (out or proc.stderr or "").strip()[:500]
        raise DeployError(
            f"namecheap DNS repoint of {domain} -> {ip} failed: {detail}"
        )
    return {
        "domain": domain,
        "host": host,
        "ip": ip,
        "ok": True,
        "records": parsed.get("records"),
    }


def _dns_resolves_to(domain: str, ip: str) -> bool:
    try:
        infos = socket.getaddrinfo(domain, None)
    except OSError:
        return False
    return any(info[4][0] == ip for info in infos)


def _wait_for_dns(
    domain: str, ip: str, *, timeout: float, interval: float
) -> bool:
    """Block until ``domain`` resolves to ``ip`` or ``timeout`` elapses.

    Returns ``True`` once resolved, ``False`` on timeout. A timeout is non-fatal
    (Caddy retries ACME on its own and public resolvers can lag the registrar).
    """
    deadline = time.monotonic() + timeout
    while True:
        if _dns_resolves_to(domain, ip):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _check_public_health(
    domain: str, *, attempts: int = 12, interval: float = 10.0
) -> dict[str, Any]:
    """Check ``https://<domain>/healthz``, retrying while TLS issues (soft)."""
    url = f"https://{domain}/healthz"
    last = ""
    for attempt in range(attempts):
        proc = subprocess.run(
            ["curl", "-fsS", "--max-time", "20", url],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return {"reachable": True, "url": url, "body": proc.stdout.strip()[:300]}
        last = (proc.stderr or proc.stdout or "").strip()[:200]
        if attempt < attempts - 1:
            time.sleep(interval)
    return {"reachable": False, "url": url, "detail": last}


def _next_steps(
    ip: str, mcp_url: str, public_health: dict[str, Any] | None
) -> list[str]:
    steps = [f"VPS provisioned/updated at {ip}."]
    if public_health and public_health.get("reachable"):
        steps.append(f"Paste {mcp_url} into a Claude Cowork connector.")
    else:
        steps.append(
            f"App is up locally but {mcp_url} did not pass a public health "
            "check yet (TLS may still be issuing); recheck in ~30s or inspect "
            "`journalctl -u caddy` on the VPS."
        )
    steps.append("Run `python -m deploy.deploy destroy` to tear the VPS down.")
    return steps


def run_destroy(args: argparse.Namespace) -> dict[str, Any]:
    """Destroy the recorded instance and clear local state."""
    state = load_state(args.state_file)
    instance_id = state.get("instance_id")
    if not instance_id:
        raise DeployError("no recorded instance to destroy")

    secrets = _load_secrets(args, require=False)
    secrets.require(CLOUDZY_TOKEN_KEY)

    client = CloudzyClient(token=secrets.cloudzy_api_token)
    try:
        result = client.destroy_instance(instance_id)
    finally:
        client.close()

    save_state({}, args.state_file)
    return {
        "ok": True,
        "command": "destroy",
        "instance_id": instance_id,
        "result": result,
    }


# -- error envelope ----------------------------------------------------------


def _error_envelope(command: str, exc: Exception) -> dict[str, Any]:
    """Build a normalized error envelope. Never includes raw secrets."""
    if isinstance(exc, CloudzyTimeoutError):
        code, retryable = "timeout", True
    elif isinstance(exc, CloudzyError):
        code, retryable = "cloudzy_error", False
    elif isinstance(exc, DeploySecretError):
        code, retryable = "missing_secret", False
    elif isinstance(exc, DeployError):
        code, retryable = "deploy_error", True
    else:
        code, retryable = "unexpected_error", True

    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        details["status_code"] = status_code
    # Cloudzy error payloads carry no secrets (validation detail/codes), so
    # surfacing them aids diagnosis of a failed provision.
    payload = getattr(exc, "payload", None)
    if payload is not None:
        details["payload"] = payload

    return {
        "ok": False,
        "command": command,
        "error": {
            "code": code,
            "message": str(exc),
            "retryable": retryable,
            "details": details,
        },
    }


# -- output ------------------------------------------------------------------


def emit_json(payload: Any) -> None:
    """Write exactly one JSON document to stdout."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _emit_human(envelope: dict[str, Any]) -> None:
    if envelope.get("dry_run"):
        plan = envelope.get("plan", {})
        print(f"[dry-run] {plan.get('action', 'deploy')} plan:")
        for i, step in enumerate(plan.get("steps", []), 1):
            print(f"  {i}. {step}")
        return
    if not envelope.get("ok", False):
        err = envelope.get("error", {})
        print(f"error [{err.get('code')}]: {err.get('message')}", file=sys.stderr)
        return
    if envelope.get("command") == "deploy":
        print(f"instance_id: {envelope.get('instance_id')}")
        print(f"ip:          {envelope.get('ip')}")
        print(f"domain:      {envelope.get('domain')}")
        print(f"mcp_url:     {envelope.get('mcp_url')}")
        health = envelope.get("public_health") or {}
        print(f"public:      {'OK' if health.get('reachable') else 'pending TLS'}")
        for step in envelope.get("next_steps", []):
            print(f"  - {step}")
    else:
        print(f"destroyed instance {envelope.get('instance_id')}")


# -- argument parsing --------------------------------------------------------


class _JsonArgumentParser(argparse.ArgumentParser):
    """Argparse parser that reports usage errors as JSON to stdout."""

    def error(self, message: str) -> None:
        emit_json(
            {
                "ok": False,
                "command": self.prog,
                "error": {
                    "code": "usage_error",
                    "message": message,
                    "retryable": False,
                    "details": {"usage": self.format_usage().strip()},
                },
            }
        )
        raise SystemExit(2)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print a JSON plan of the deploy without contacting Cloudzy/SSH/"
            "DNS or requiring a token; exits 0."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON document to stdout (otherwise human-readable).",
    )
    parser.add_argument(
        "--app-dir",
        default=DEFAULT_APP_DIR,
        help=f"Remote deployment directory (default: {DEFAULT_APP_DIR}).",
    )
    parser.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
        help=f"Unprivileged service user (default: {DEFAULT_SERVICE_USER}).",
    )
    parser.add_argument(
        "--app-port",
        type=int,
        default=DEFAULT_APP_PORT,
        help=f"Port the ASGI app binds to (default: {DEFAULT_APP_PORT}).",
    )
    parser.add_argument(
        "--remote-env-file",
        default=None,
        help="Remote env file path (default: <app_dir>/.env).",
    )
    parser.add_argument(
        "--ssh-user",
        default=DEFAULT_SSH_USER,
        help=f"SSH login user (default: {DEFAULT_SSH_USER}).",
    )
    parser.add_argument(
        "--ssh-key-file",
        default=None,
        help="Path to the SSH private key used to connect.",
    )
    parser.add_argument(
        "--deploy-env-file",
        default=None,
        help="Override the deploy secret env file path.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=f"Local deployment state file (default: {STATE_FILE}).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="legal-deploy",
        description="End-to-end deployment orchestrator for the legal VPS.",
    )
    subparsers = parser.add_subparsers(dest="command")

    deploy = subparsers.add_parser(
        "deploy", help="Provision-or-reuse a VPS and deploy the app."
    )
    _add_common_flags(deploy)
    deploy.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore recorded state and provision a new instance.",
    )
    deploy.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help=(
            "Public domain Caddy fronts; also the bare connector URL "
            f"https://<domain> (default: {DEFAULT_DOMAIN})."
        ),
    )
    deploy.add_argument(
        "--no-dns",
        action="store_true",
        help=(
            "Skip the automatic Namecheap A-record repoint on --fresh "
            "(point DNS at the new IP yourself, then re-run deploy)."
        ),
    )
    deploy.add_argument(
        "--dns-timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for DNS to resolve to a fresh IP (default: 600).",
    )
    deploy.add_argument(
        "--dns-interval",
        type=float,
        default=15.0,
        help="DNS-resolution poll interval in seconds (default: 15).",
    )
    deploy.add_argument("--region", default=DEFAULT_REGION, help="Cloudzy region id.")
    deploy.add_argument(
        "--product", default=DEFAULT_PRODUCT, help="Cloudzy product/plan id."
    )
    deploy.add_argument(
        "--os", default=DEFAULT_OS, help="Operating system image id."
    )
    deploy.add_argument(
        "--hostname", default=DEFAULT_HOSTNAME, help="Instance hostname."
    )
    deploy.add_argument(
        "--ssh-key",
        action="append",
        default=None,
        help="Cloudzy SSH key id to attach (repeatable).",
    )
    deploy.add_argument("--label", default=None, help="Instance label.")
    deploy.add_argument(
        "--allowed-email",
        default=DEFAULT_ALLOWED_EMAIL,
        help=(
            "Email permitted to authenticate to the MCP server "
            f"(default: {DEFAULT_ALLOWED_EMAIL})."
        ),
    )
    deploy.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Instance readiness poll timeout in seconds (default: 600).",
    )
    deploy.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Instance readiness poll interval in seconds (default: 10).",
    )
    deploy.add_argument(
        "--ssh-timeout",
        type=float,
        default=DEFAULT_SSH_TIMEOUT,
        help="SSH-reachability wait timeout in seconds (default: 600).",
    )
    deploy.add_argument(
        "--ssh-interval",
        type=float,
        default=DEFAULT_SSH_INTERVAL,
        help="SSH-reachability poll interval in seconds (default: 10).",
    )
    deploy.set_defaults(func=run_deploy)

    destroy = subparsers.add_parser(
        "destroy", help="Destroy the recorded VPS and clear local state."
    )
    _add_common_flags(destroy)
    destroy.set_defaults(func=run_destroy)

    return parser


def _normalize_argv(argv: Sequence[str] | None) -> list[str]:
    """Default to the ``deploy`` subcommand when none is given.

    A bare invocation like ``--dry-run --json`` (or no args) is treated as a
    ``deploy`` invocation so the orchestrator's common flags are available
    without forcing the subcommand name. ``--help`` is passed through untouched
    so argparse can print top-level help and exit 0.
    """
    items = list(argv) if argv is not None else list(sys.argv[1:])
    if not items:
        return ["deploy"]
    first = items[0]
    if first in ("-h", "--help") or first in ("deploy", "destroy"):
        return items
    return ["deploy", *items]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))

    command = getattr(args, "command", None)
    if command is None:
        command = "deploy"

    # Critical: dry-run never touches network / SSH / token.
    if getattr(args, "dry_run", False):
        envelope = {
            "ok": True,
            "dry_run": True,
            "command": command,
            "plan": build_plan(args),
        }
        if getattr(args, "json", False):
            emit_json(envelope)
        else:
            _emit_human(envelope)
        return 0

    try:
        envelope = args.func(args)
    except Exception as exc:  # normalized error envelope; one JSON doc out
        envelope = _error_envelope(command, exc)

    if getattr(args, "json", False):
        emit_json(envelope)
    else:
        _emit_human(envelope)
    return 0 if envelope.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
