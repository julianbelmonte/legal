"""End-to-end deployment orchestrator for the legal API + MCP server VPS.

Runnable as ``python -m legal_deploy.deploy``. This module composes the existing
deploy building blocks into a single deploy flow:

- :mod:`legal_deploy.cloudzy` (``CloudzyClient``) to provision / reuse / poll /
  destroy a Cloudzy VPS,
- :mod:`legal_deploy.secrets` (``load_deploy_secrets`` / ``redact_secret``) to
  resolve the Cloudzy token + ngrok authtoken without ever printing raw values,
- :mod:`legal_deploy.bootstrap` (``render_bootstrap_script`` /
  ``render_systemd_units`` / ``render_env_file``) to render the remote setup.

The deploy flow: provision-or-reuse a VPS, wait for SSH (paramiko), sync the
repo into ``app_dir``, write the remote env file (chmod 600) from the secret
loader, run the bootstrap script, ``uv sync``, start the systemd services,
verify health, and record deployment state locally under
``~/.config/legal-agent/deploy-state.json``. ``destroy`` tears down a recorded
instance.

``--dry-run`` is the critical safe mode: with ``--dry-run`` (and ``--json``) the
command contacts no network, requires no token, opens no SSH, and prints exactly
one JSON document describing the ordered plan, target ``app_dir`` /
``service_user``, and the rendered command summary, then exits 0.

This is standalone deploy tooling and does not import the legal pipeline's
source-access internals.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as _secrets_mod
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from legal_deploy.bootstrap import (
    APP_SERVICE_NAME,
    DEFAULT_APP_PORT,
    NGROK_SERVICE_NAME,
    render_bootstrap_script,
    render_env_file,
    render_systemd_units,
)
from legal_deploy.cloudzy import (
    CloudzyClient,
    CloudzyError,
    CloudzyTimeoutError,
    CreateInstanceRequest,
    created_instance_id,
)
from legal_deploy.ngrok import oauth_env_updates
from legal_deploy.secrets import (
    CLOUDZY_TOKEN_KEY,
    NGROK_AUTHTOKEN_ENV_VAR,
    DeploySecretError,
    load_deploy_secrets,
    redact_secret,
)

# --- MCP / OAuth env keys written to the remote env file --------------------

MCP_SIGNING_KEY = "LEGAL_MCP_OAUTH_SIGNING_KEY"
MCP_LOGIN_SECRET = "LEGAL_MCP_OAUTH_LOGIN_SECRET"
MCP_ALLOWED_EMAILS = "LEGAL_MCP_ALLOWED_EMAILS"
MCP_AUTH_ENABLED = "LEGAL_MCP_AUTH_ENABLED"
API_KEY_ENV = "LEGAL_API_KEY"

#: Default allowed email for the single-user MCP login.
DEFAULT_ALLOWED_EMAIL = "ayacuchovictor@gmail.com"

# -- defaults ----------------------------------------------------------------

#: Remote deployment directory holding the synced repo.
DEFAULT_APP_DIR = "/opt/legal-agent"
#: Unprivileged service user that owns/runs the systemd services.
DEFAULT_SERVICE_USER = "legal"
#: Remote env file loaded by the app systemd unit (chmod 600).
DEFAULT_REMOTE_ENV_FILE = str(PurePosixPath(DEFAULT_APP_DIR) / ".env")

#: Default Cloudzy provisioning selectors (overridable on the CLI). These are
#: validated live IDs from the Cloudzy Developer API: a US-Las-Vegas region, a
#: 4 GB / 2 vCPU default plan (enough headroom for uv sync + BotBrowser vendor),
#: and the Ubuntu Server 24.04 LTS image. Discover current IDs with
#: ``python -m legal_deploy.cloudzy_cli regions|products|os``.
DEFAULT_REGION = "US-Las-Vegas"
DEFAULT_PRODUCT = "2d798f98-d0e1-4b78-ba5c-663b2212bfb8"
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
    rendered_env_keys = [CLOUDZY_TOKEN_KEY, "LEGAL_CAPSOLVER_API_KEY"]
    remote_commands = [
        f"ssh {args.ssh_user}@<ip> : run rendered bootstrap.sh "
        "(apt deps, uv, ngrok, service user, uv sync, vendor profiles, "
        "systemd units)",
        "ssh : write remote env file via render_env_file (chmod 600)",
        f"systemctl enable --now {APP_SERVICE_NAME}.service "
        f"{NGROK_SERVICE_NAME}.service",
        f"curl http://127.0.0.1:{app_port}/healthz : verify API health",
        "query ngrok local API for the public tunnel URL",
    ]

    steps = [
        "load deploy secrets (Cloudzy token + ngrok authtoken)",
        (
            "reuse recorded instance"
            if (not args.fresh and load_state(args.state_file).get("instance_id"))
            else "provision a new Cloudzy instance"
        ),
        "poll the instance until it reaches a ready state",
        f"wait for SSH on port {SSH_PORT} (paramiko)",
        f"sync the repo into {app_dir} (rsync over ssh)",
        f"write remote env file {remote_env_file} (chmod 600)",
        "run the rendered bootstrap script remotely",
        f"uv sync in {app_dir}",
        f"start systemd services ({', '.join(units)})",
        f"verify API health at /healthz on port {app_port}",
        "discover the ngrok public URL",
        "record deployment state locally",
    ]

    return {
        "action": "deploy",
        "app_dir": app_dir,
        "service_user": service_user,
        "app_port": app_port,
        "remote_env_file": remote_env_file,
        "ssh_user": args.ssh_user,
        "region": args.region,
        "product": args.product,
        "hostname": args.hostname,
        "fresh": bool(args.fresh),
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
        ngrok_config_file=args.ngrok_config_file,
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
        "--exclude",
        "legal/vendor",
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
    allowed_email: str,
    reuse: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build the remote env mapping (secrets + MCP/OAuth config).

    Generates strong OAuth signing/login secrets and a ``LEGAL_API_KEY`` the
    first time, reusing values recorded in ``reuse`` (the prior deploy state) on
    a redeploy so issued tokens stay valid. The public URL / issuer are set
    later, once the ngrok tunnel is discovered.
    """
    reuse = reuse or {}
    env: dict[str, str] = {}
    if secrets.cloudzy_api_token:
        env[CLOUDZY_TOKEN_KEY] = secrets.cloudzy_api_token
    for key, value in secrets.extra.items():
        if value:
            env[key] = value

    env[MCP_AUTH_ENABLED] = "true"
    env[MCP_ALLOWED_EMAILS] = allowed_email
    env[MCP_SIGNING_KEY] = reuse.get(MCP_SIGNING_KEY) or _secrets_mod.token_urlsafe(48)
    env[MCP_LOGIN_SECRET] = (
        reuse.get(MCP_LOGIN_SECRET) or _secrets_mod.token_urlsafe(24)
    )
    env[API_KEY_ENV] = reuse.get(API_KEY_ENV) or _secrets_mod.token_urlsafe(24)
    return env


def _append_remote_env(client_ssh: Any, env_file: str, updates: dict[str, str]) -> None:
    """Append ``KEY=VALUE`` updates to the remote env file (idempotent-ish).

    Replaces any existing line for each key, then appends the new value, keeping
    the file at chmod 600. Values are single-quoted for the shell heredoc.
    """
    if not updates:
        return
    q_file = shlex.quote(env_file)
    lines = [f"touch {q_file}", f"chmod 600 {q_file}"]
    for key, value in updates.items():
        # Drop any prior definition, then append the new one.
        lines.append(f"sed -i {shlex.quote(f'/^{key}=/d')} {q_file}")
        lines.append(f"printf '%s\\n' {shlex.quote(f'{key}={value}')} >> {q_file}")
    script = "\n".join(lines)
    code, _out, err = _run_remote(
        client_ssh, f"bash -s <<'LEGAL_DEPLOY_ENVUP_EOF'\n{script}\nLEGAL_DEPLOY_ENVUP_EOF"
    )
    if code != 0:
        raise DeployError(f"remote env update failed (exit {code}): {err.strip()}")


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
            request = CreateInstanceRequest(
                region=args.region,
                product=args.product,
                operating_system=args.os,
                hostname=args.hostname,
                ssh_keys=list(args.ssh_key or []),
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

    wait_for_ssh(
        ip, timeout=args.ssh_timeout, interval=args.ssh_interval
    )

    remote_env_file = args.remote_env_file or str(
        PurePosixPath(args.app_dir) / ".env"
    )

    allowed_email = getattr(args, "allowed_email", None) or DEFAULT_ALLOWED_EMAIL
    # Build the full remote env: deploy secrets + MCP/OAuth config. Reuse the
    # signing key / login secret / API key from prior state on a redeploy so any
    # already-issued bearer token keeps validating.
    env_mapping = _build_mcp_env(
        secrets, allowed_email=allowed_email, reuse=state
    )
    # Persist the OAuth signing config locally so smoke_codex / the orchestrator
    # can mint a bearer token without a browser flow.
    signing_key = env_mapping[MCP_SIGNING_KEY]

    bootstrap_script = render_bootstrap_script(
        app_dir=args.app_dir,
        service_user=args.service_user,
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
    ngrok_url: str | None = None
    try:
        # Ensure the app dir exists before syncing.
        _run_remote(client_ssh, f"mkdir -p {args.app_dir}")
        _sync_repo(
            ip,
            user=args.ssh_user,
            app_dir=args.app_dir,
            key_path=args.ssh_key_file,
        )

        # Write env file (chmod 600) then run bootstrap (installs deps incl.
        # ngrok + uv, uv sync, vendors BotBrowser, installs + starts the systemd
        # services).
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

        # Configure ngrok's authtoken AFTER bootstrap has installed ngrok. The
        # ngrok systemd unit runs as the service user, so the authtoken must
        # live in THAT user's ngrok config; then restart the unit so the tunnel
        # authenticates. The token never appears in logged output.
        if secrets.ngrok_authtoken:
            home = f"/home/{args.service_user}"
            _run_remote(
                client_ssh,
                "sudo -u {u} env HOME={h} ngrok config add-authtoken {t} "
                "|| ngrok config add-authtoken {t} || true".format(
                    u=shlex.quote(args.service_user),
                    h=shlex.quote(home),
                    t=shlex.quote(secrets.ngrok_authtoken),
                ),
            )
            _run_remote(
                client_ssh,
                f"systemctl restart {NGROK_SERVICE_NAME}.service || true",
            )

        # Discover the ngrok public URL from the local agent API, retrying while
        # the tunnel comes up.
        ngrok_url = _discover_ngrok_url(client_ssh)

        # Point the runtime OAuth metadata / MCP public URL at the tunnel, then
        # restart the app so the new env takes effect.
        if ngrok_url:
            _append_remote_env(
                client_ssh, remote_env_file, oauth_env_updates(ngrok_url)
            )
            _run_remote(
                client_ssh,
                f"systemctl restart {APP_SERVICE_NAME}.service || true",
            )
            time.sleep(5)

        # Verify health (after restart).
        code, out, _ = _run_remote(
            client_ssh,
            f"curl -fsS http://127.0.0.1:{args.app_port}/healthz || true",
        )
        api_health = {"reachable": code == 0, "body": out.strip()[:500]}
    finally:
        client_ssh.close()

    mcp_url = f"{ngrok_url}/mcp" if ngrok_url else None
    issuer = oauth_env_updates(ngrok_url)["LEGAL_MCP_OAUTH_ISSUER"] if ngrok_url else None

    new_state = {
        "instance_id": instance_id,
        "ip": ip,
        "app_dir": args.app_dir,
        "service_user": args.service_user,
        "app_port": args.app_port,
        "remote_env_file": remote_env_file,
        "ngrok_url": ngrok_url,
        "mcp_url": mcp_url,
        "ssh_user": args.ssh_user,
        "allowed_email": allowed_email,
        # OAuth config needed to mint a bearer token for the smoke. The state
        # file is written chmod 600 (local secret), same posture as deploy.env.
        MCP_SIGNING_KEY: signing_key,
        MCP_ALLOWED_EMAILS: allowed_email,
        MCP_LOGIN_SECRET: env_mapping[MCP_LOGIN_SECRET],
        API_KEY_ENV: env_mapping[API_KEY_ENV],
        "oauth_issuer": issuer,
        "oauth_resource": mcp_url,
    }
    state_path = save_state(new_state, args.state_file)

    return {
        "ok": True,
        "command": "deploy",
        "instance_id": instance_id,
        "ip": ip,
        "ngrok_url": ngrok_url,
        "mcp_url": mcp_url,
        "api_health": api_health,
        "state_file": str(state_path),
        "secrets": secrets.diagnostics(),
        "next_steps": _next_steps(ip, ngrok_url, mcp_url),
    }


def _discover_ngrok_url(
    client_ssh: Any, *, attempts: int = 30, interval: float = 5.0
) -> str | None:
    """Poll the remote ngrok agent API for the public https tunnel URL."""
    for _ in range(attempts):
        code, out, _ = _run_remote(
            client_ssh,
            "curl -fsS http://127.0.0.1:4040/api/tunnels || true",
        )
        url = _parse_ngrok_url(out)
        if url:
            return url
        time.sleep(interval)
    return None


def _parse_ngrok_url(body: str) -> str | None:
    if not body.strip():
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    tunnels = data.get("tunnels") if isinstance(data, dict) else None
    if not isinstance(tunnels, list):
        return None
    https = None
    for tunnel in tunnels:
        url = tunnel.get("public_url") if isinstance(tunnel, dict) else None
        if isinstance(url, str):
            if url.startswith("https://"):
                return url
            https = https or url
    return https


def _next_steps(ip: str, ngrok_url: str | None, mcp_url: str | None) -> list[str]:
    steps = [f"VPS provisioned at {ip}."]
    if mcp_url:
        steps.append(f"Connect your MCP client to {mcp_url}.")
    else:
        steps.append(
            "ngrok URL not yet available; re-run deploy or check the "
            "legal-ngrok service on the VPS."
        )
    steps.append(
        "Run `python -m legal_deploy.deploy destroy` to tear the VPS down."
    )
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
        print(f"ngrok_url:   {envelope.get('ngrok_url')}")
        print(f"mcp_url:     {envelope.get('mcp_url')}")
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
            "ngrok or requiring a token; exits 0."
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
        "--ngrok-config-file",
        default=None,
        help="Override the ngrok config file path.",
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
