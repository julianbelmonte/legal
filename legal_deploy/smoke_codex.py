"""Remote smoke test that drives Codex CLI against a deployed MCP server.

Runnable as ``python -m legal_deploy.smoke_codex``. Before testing Claude
Cowork, the deployed remote MCP server (the ``/mcp`` streamable-HTTP endpoint
fronted by ngrok) should be exercised from this machine through an agent CLI.
This command does exactly that: it writes a **temporary** Codex MCP config
pointing at ``--server-url`` (with an optional OAuth/bearer token), then invokes
the Codex CLI to

1. list the MCP tools exposed by the server,
2. call ``legal_sources`` (cheap discovery, no source egress), and
3. run a small ``legal_search`` / discovery request.

Captured stdout/stderr is returned for diagnosis. Secrets (the bearer token)
are **never** printed: every reportable rendering of the config or environment
runs the token through :func:`legal_deploy.secrets.redact_secret`.

``--dry-run`` renders the PLAN -- the temp config that *would* be written (with
the token redacted), the codex command(s) that *would* run, and the smoke steps
-- without contacting the network, requiring a token, or needing the codex
binary installed. It prints a JSON document and exits 0.

On failure the non-dry-run path returns a structured result that includes the
captured diagnostics, so the deploy workflow can collect service logs, ngrok
status, OAuth metadata, and MCP challenge responses, then retry after fixes.

This is standalone deploy tooling: it does not import the legal pipeline's
source-access internals.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from legal_deploy.secrets import redact_secret

#: Env var the bearer token is read from when ``--bearer-token`` is not given.
BEARER_TOKEN_ENV_VAR = "LEGAL_MCP_BEARER_TOKEN"

#: Env var holding the remote MCP URL (used by callers/acceptance, not required
#: here -- the URL is passed explicitly via ``--server-url``).
REMOTE_URL_ENV_VAR = "LEGAL_MCP_REMOTE_URL"

#: Name of the MCP server entry written into the temporary Codex config.
MCP_SERVER_NAME = "legal"

#: Default Codex executable.
DEFAULT_CODEX_BIN = "codex"

#: The smoke steps, in order. Each is a single Codex prompt run.
SMOKE_STEPS: tuple[dict[str, str], ...] = (
    {
        "id": "list_tools",
        "description": "List the MCP tools exposed by the remote server.",
        "prompt": (
            f"List every tool exposed by the '{MCP_SERVER_NAME}' MCP server. "
            "Report the tool names only."
        ),
    },
    {
        "id": "legal_sources",
        "description": "Call legal_sources to enumerate wired sources.",
        "prompt": (
            f"Call the {MCP_SERVER_NAME} MCP tool 'legal_sources' with no "
            "arguments and report the source ids it returns."
        ),
    },
    {
        "id": "legal_search",
        "description": "Run a small cross-source legal_search discovery request.",
        "prompt": (
            f"Call the {MCP_SERVER_NAME} MCP tool 'legal_search' with a small "
            "query (text 'ley 26076', limit 1) and report whether it returned "
            "without error."
        ),
    },
)


class SmokeCodexError(RuntimeError):
    """Raised for unrecoverable smoke-test setup problems (not codex failures)."""


def _normalize_server_url(url: str) -> str:
    """Return the trimmed server URL, ensuring it targets the ``/mcp`` path.

    The deployed remote endpoint ends in ``/mcp``; we keep the URL as given but
    strip surrounding whitespace and any trailing slash so the config is stable.
    """
    base = url.strip().rstrip("/")
    if not base:
        raise SmokeCodexError("--server-url must be a non-empty URL")
    return base


def resolve_bearer_token(arg_token: str | None) -> str | None:
    """Resolve the bearer token from the flag, then the environment.

    Returns ``None`` when no token is configured (the server may use anonymous
    or out-of-band OAuth). The raw value is never logged by callers.
    """
    if arg_token:
        return arg_token
    env_token = os.environ.get(BEARER_TOKEN_ENV_VAR)
    if env_token:
        return env_token
    return None


def build_codex_config(server_url: str, bearer_token: str | None) -> dict[str, Any]:
    """Build the Codex MCP server config mapping for ``server_url``.

    Codex addresses a remote streamable-HTTP MCP server by ``url``; the bearer
    token (when present) is sent as an ``Authorization: Bearer <token>`` header.
    The returned mapping holds the RAW token -- serialize via
    :func:`redact_config` for anything reportable.
    """
    server: dict[str, Any] = {"url": server_url}
    if bearer_token:
        server["bearer_token"] = bearer_token
        server["http_headers"] = {"Authorization": f"Bearer {bearer_token}"}
    return {"mcp_servers": {MCP_SERVER_NAME: server}}


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a Codex config with the bearer token redacted.

    Safe to print/log: the raw token never appears in the result.
    """
    servers = config.get("mcp_servers", {})
    out_servers: dict[str, Any] = {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            out_servers[name] = server
            continue
        safe = dict(server)
        if "bearer_token" in safe:
            safe["bearer_token"] = redact_secret(safe.get("bearer_token"))
        headers = safe.get("http_headers")
        if isinstance(headers, dict) and "Authorization" in headers:
            new_headers = dict(headers)
            new_headers["Authorization"] = "Bearer " + redact_secret(
                _strip_bearer(headers["Authorization"])
            )
            safe["http_headers"] = new_headers
        out_servers[name] = safe
    return {**config, "mcp_servers": out_servers}


def _strip_bearer(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def render_config_toml(config: dict[str, Any]) -> str:
    """Render a Codex config mapping as TOML (``config.toml`` format).

    Codex reads its MCP servers from ``~/.codex/config.toml``; the temp config
    written for a smoke run uses the same ``[mcp_servers.<name>]`` shape. Only
    the small subset of types used here (str / nested table) is supported.
    """
    lines: list[str] = []
    for name, server in config.get("mcp_servers", {}).items():
        lines.append(f"[mcp_servers.{name}]")
        nested: list[tuple[str, dict[str, Any]]] = []
        for key, value in server.items():
            if isinstance(value, dict):
                nested.append((key, value))
                continue
            lines.append(f"{key} = {_toml_scalar(value)}")
        for key, value in nested:
            lines.append("")
            lines.append(f"[mcp_servers.{name}.{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{_toml_key(sub_key)} = {_toml_scalar(sub_value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _toml_string(str(value))


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_key(key: str) -> str:
    # Quote keys that are not bare-key safe (e.g. "Authorization" is fine, but
    # header names can contain '-').
    if key and all(c.isalnum() or c in "-_" for c in key):
        return key
    return _toml_string(key)


def _codex_commands(codex_bin: str, config_path: str) -> list[list[str]]:
    """Return the codex commands run for each smoke step.

    Each step runs ``codex exec`` non-interactively with the temp config so the
    smoke is fully scripted (no TTY). The config carries the MCP server wiring.
    """
    commands: list[list[str]] = []
    for step in SMOKE_STEPS:
        commands.append(
            [
                codex_bin,
                "--config",
                config_path,
                "exec",
                "--skip-git-repo-check",
                step["prompt"],
            ]
        )
    return commands


def _plan(
    *,
    server_url: str,
    bearer_token: str | None,
    codex_bin: str,
    config_path: str,
) -> dict[str, Any]:
    """Build the dry-run plan (no network, no token required, no codex needed)."""
    config = build_codex_config(server_url, bearer_token)
    safe_config = redact_config(config)
    return {
        "server_url": server_url,
        "mcp_server_name": MCP_SERVER_NAME,
        "bearer_token": redact_secret(bearer_token),
        "bearer_token_present": bool(bearer_token),
        "codex_bin": codex_bin,
        "temp_config_path": config_path,
        "temp_config": safe_config,
        "temp_config_toml": render_config_toml(safe_config),
        "commands": [
            {"step": step["id"], "description": step["description"], "argv": argv}
            for step, argv in zip(SMOKE_STEPS, _codex_commands(codex_bin, config_path))
        ],
        "steps": [
            {"id": s["id"], "description": s["description"]} for s in SMOKE_STEPS
        ],
    }


def _run_step(argv: Sequence[str], *, timeout: float) -> dict[str, Any]:
    """Run one codex command, capturing stdout/stderr and the exit code."""
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": f"codex binary not found: {exc}",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "error": f"codex timed out after {timeout}s",
        }
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_smoke(
    *,
    server_url: str,
    bearer_token: str | None,
    codex_bin: str,
    timeout: float,
) -> dict[str, Any]:
    """Run the live smoke: write the temp config, run codex per step.

    Returns a structured result. On failure the result includes per-step
    captured diagnostics so the deploy workflow can collect service logs / ngrok
    status / OAuth metadata / MCP challenge and retry. The bearer token is never
    placed in the returned structure (only its redacted preview).
    """
    config = build_codex_config(server_url, bearer_token)
    safe_config = redact_config(config)
    config_toml = render_config_toml(config)

    tmp_dir = tempfile.mkdtemp(prefix="legal-smoke-codex-")
    config_path = str(Path(tmp_dir) / "config.toml")
    Path(config_path).write_text(config_toml, encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass

    results: list[dict[str, Any]] = []
    all_ok = True
    try:
        for step, argv in zip(SMOKE_STEPS, _codex_commands(codex_bin, config_path)):
            step_result = _run_step(argv, timeout=timeout)
            step_result["step"] = step["id"]
            step_result["description"] = step["description"]
            results.append(step_result)
            if not step_result.get("ok", False):
                all_ok = False
                # Stop on first failure: later steps depend on the connection.
                break
    finally:
        # Best-effort cleanup of the temp config (it holds the raw token).
        try:
            Path(config_path).unlink(missing_ok=True)
            os.rmdir(tmp_dir)
        except OSError:
            pass

    envelope: dict[str, Any] = {
        "ok": all_ok,
        "command": "smoke_codex",
        "server_url": server_url,
        "mcp_server_name": MCP_SERVER_NAME,
        "bearer_token": redact_secret(bearer_token),
        "bearer_token_present": bool(bearer_token),
        "codex_bin": codex_bin,
        "temp_config": safe_config,
        "steps": results,
    }
    if not all_ok:
        envelope["diagnostics_to_collect"] = [
            "service logs (systemd journal for the api/mcp unit)",
            "ngrok status (legal_deploy.ngrok.discover_public_url / agent API)",
            "OAuth metadata (.well-known/oauth-authorization-server)",
            "MCP challenge response (401 WWW-Authenticate on /mcp)",
        ]
        envelope["error"] = {
            "code": "smoke_failed",
            "message": "remote Codex MCP smoke failed; collect diagnostics and retry",
            "retryable": True,
        }
    return envelope


def emit_json(payload: Any) -> None:
    """Write exactly one JSON document to stdout."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def emit_text_plan(plan: dict[str, Any]) -> None:
    """Print a human-readable rendering of the dry-run plan."""
    print(f"server_url: {plan['server_url']}")
    print(f"mcp_server_name: {plan['mcp_server_name']}")
    print(f"bearer_token: {plan['bearer_token']}")
    print(f"codex_bin: {plan['codex_bin']}")
    print(f"temp_config_path: {plan['temp_config_path']}")
    print("temp_config (redacted) TOML:")
    for line in plan["temp_config_toml"].splitlines():
        print(f"  {line}")
    print("commands:")
    for cmd in plan["commands"]:
        print(f"  [{cmd['step']}] {' '.join(cmd['argv'])}")
    print("steps:")
    for step in plan["steps"]:
        print(f"  - {step['id']}: {step['description']}")


class _ExitOkParser(argparse.ArgumentParser):
    """Parser whose ``--help`` exits 0 (argparse default) and which keeps the
    standard exit-2 on usage errors."""


def build_parser() -> argparse.ArgumentParser:
    parser = _ExitOkParser(
        prog="smoke_codex",
        description=(
            "Remote smoke test: drive Codex CLI against a deployed MCP server "
            "(list tools, call legal_sources, run a small legal_search)."
        ),
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help=(
            "Deployed MCP server URL (ending in /mcp). Required unless "
            f"--dry-run reads it from {REMOTE_URL_ENV_VAR}."
        ),
    )
    parser.add_argument(
        "--bearer-token",
        "--token",
        dest="bearer_token",
        default=None,
        help=(
            "OAuth/bearer token for the MCP endpoint (otherwise read from "
            f"{BEARER_TOKEN_ENV_VAR}). Never printed."
        ),
    )
    parser.add_argument(
        "--codex-bin",
        default=DEFAULT_CODEX_BIN,
        help=f"Codex CLI executable (default: {DEFAULT_CODEX_BIN}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-step codex timeout in seconds (default: 180).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render the plan (temp config, codex commands, smoke steps) without "
            "contacting the network, requiring a token, or needing codex; "
            "exits 0."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Force JSON output (default for the live run; optional for dry-run).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    server_url_raw = args.server_url
    if not server_url_raw:
        server_url_raw = os.environ.get(REMOTE_URL_ENV_VAR)
    if not server_url_raw:
        # Usage error: argparse-style exit 2 with a JSON or text message.
        message = (
            "--server-url is required (or set "
            f"{REMOTE_URL_ENV_VAR} in the environment)"
        )
        if args.json:
            emit_json(
                {
                    "ok": False,
                    "command": "smoke_codex",
                    "error": {
                        "code": "usage_error",
                        "message": message,
                        "retryable": False,
                    },
                }
            )
        else:
            print(message, file=sys.stderr)
        return 2

    try:
        server_url = _normalize_server_url(server_url_raw)
    except SmokeCodexError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # --dry-run resolves the token only to compute a redacted preview; it never
    # requires one and never contacts the network or codex.
    bearer_token = resolve_bearer_token(args.bearer_token)

    if args.dry_run:
        # In dry-run we never write a real file; show a representative path.
        config_path = str(
            Path(tempfile.gettempdir()) / "legal-smoke-codex-XXXX" / "config.toml"
        )
        plan = _plan(
            server_url=server_url,
            bearer_token=bearer_token,
            codex_bin=args.codex_bin,
            config_path=config_path,
        )
        envelope = {
            "ok": True,
            "dry_run": True,
            "command": "smoke_codex",
            "plan": plan,
        }
        if args.json:
            emit_json(envelope)
        else:
            print("DRY RUN -- no network, no codex, no token required")
            emit_text_plan(plan)
        return 0

    envelope = run_smoke(
        server_url=server_url,
        bearer_token=bearer_token,
        codex_bin=args.codex_bin,
        timeout=args.timeout,
    )
    emit_json(envelope)
    return 0 if envelope.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
