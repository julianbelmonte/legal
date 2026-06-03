"""Command-line interface for Cloudzy VPS provisioning and destruction.

Runnable as ``python -m deploy.cloudzy_cli``. Every subcommand prints
exactly one JSON document to stdout, including a normalized error envelope on
failure (a non-zero exit still prints a JSON document). This mirrors the legal
pipeline CLI's one-JSON-document-to-stdout convention.

This is standalone deploy tooling. It builds on :mod:`deploy.cloudzy` and
does not import the legal pipeline's source-access internals.

``--dry-run`` is a global flag accepted by every subcommand. In dry-run mode no
command contacts the network or requires a token: it prints a JSON plan/echo of
the action that *would* run and exits 0. This makes ``provision``/``destroy``
safe to inspect, and lets read commands be exercised without credentials.

The API token is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from deploy.cloudzy import (
    TOKEN_ENV_VAR,
    CloudzyClient,
    CloudzyError,
    CloudzyTimeoutError,
    CreateInstanceRequest,
)


def emit_json(payload: Any) -> None:
    """Write exactly one JSON document to stdout."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _ok(command: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "command": command, **extra}


def _dry_run(command: str, plan: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "dry_run": True, "command": command, "plan": plan}


def _error_envelope(command: str, exc: Exception) -> dict[str, Any]:
    """Build a normalized error envelope. Never includes the API token."""
    if isinstance(exc, CloudzyTimeoutError):
        code = "timeout"
        retryable = True
    elif isinstance(exc, CloudzyError):
        code = "cloudzy_error"
        retryable = False
    else:
        code = "unexpected_error"
        retryable = True

    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        details["status_code"] = status_code
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


def _exit_code(envelope: dict[str, Any]) -> int:
    return 0 if envelope.get("ok", False) else 1


# -- subcommand handlers ----------------------------------------------------
#
# Each read handler is only invoked when not in dry-run mode, so it may resolve
# the token and call Cloudzy. Dry-run is intercepted centrally in ``main``.


def _client(args: argparse.Namespace) -> CloudzyClient:
    return CloudzyClient(token=args.token)


def cmd_regions(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("regions", regions=client.list_regions())


def cmd_products(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("products", products=client.list_products())


def cmd_os(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("os", operating_systems=client.list_operating_systems())


def cmd_ssh_keys(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("ssh-keys", ssh_keys=client.list_ssh_keys())


def cmd_instances(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("instances", instances=client.list_instances())


def _create_request(args: argparse.Namespace) -> CreateInstanceRequest:
    return CreateInstanceRequest(
        region=args.region,
        product=args.product,
        operating_system=args.os,
        application=args.application,
        hostname=args.hostname,
        ssh_keys=list(args.ssh_key or []),
        label=args.label,
    )


def cmd_provision(args: argparse.Namespace) -> dict[str, Any]:
    request = _create_request(args)
    with _client(args) as client:
        instance = client.create_instance(request)
        result = _ok("provision", instance=instance)
        if args.wait:
            instance_id = _instance_id(instance)
            if instance_id is None:
                result["warning"] = "could not determine instance id; skipped wait"
            else:
                ready = client.wait_for_instance(
                    instance_id, timeout=args.timeout, interval=args.interval
                )
                result["instance"] = ready
                result["ready"] = True
        return result


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        ready = client.wait_for_instance(
            args.instance_id, timeout=args.timeout, interval=args.interval
        )
        return _ok("wait", instance=ready, ready=True)


def cmd_destroy(args: argparse.Namespace) -> dict[str, Any]:
    with _client(args) as client:
        return _ok("destroy", result=client.destroy_instance(args.instance_id))


def _instance_id(instance: Any) -> str | None:
    if not isinstance(instance, dict):
        return None
    for key in ("id", "instanceId", "instance_id", "uuid"):
        value = instance.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


# -- dry-run plan builders --------------------------------------------------


def _dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Describe the action that would run, without contacting the network."""
    command = args.command
    plan: dict[str, Any] = {"action": command}
    if command in {"regions", "products", "os", "ssh-keys", "instances"}:
        plan["method"] = "read"
    elif command == "provision":
        plan["method"] = "create"
        plan["request"] = _create_request(args).to_payload()
        plan["wait"] = bool(args.wait)
    elif command == "wait":
        plan["method"] = "poll"
        plan["instance_id"] = args.instance_id
        plan["timeout"] = args.timeout
        plan["interval"] = args.interval
    elif command == "destroy":
        plan["method"] = "delete"
        plan["instance_id"] = args.instance_id
    return plan


# -- argument parsing -------------------------------------------------------


class _JsonArgumentParser(argparse.ArgumentParser):
    """Argparse parser that reports usage errors as JSON to stdout.

    ``--help`` is left to argparse's default behavior, which prints help and
    exits 0.
    """

    def error(self, message: str) -> None:
        envelope = {
            "ok": False,
            "command": self.prog,
            "error": {
                "code": "usage_error",
                "message": message,
                "retryable": False,
                "details": {"usage": self.format_usage().strip()},
            },
        }
        emit_json(envelope)
        raise SystemExit(2)


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Describe the planned action as JSON without contacting the "
            "network or requiring a token; exits 0."
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Cloudzy API token (otherwise read from "
            f"{TOKEN_ENV_VAR}). Never printed."
        ),
    )


def _add_wait_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Readiness poll timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Readiness poll interval in seconds (default: 10).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="cloudzy",
        description="Cloudzy VPS provisioning and destruction CLI.",
    )
    _add_global_flags(parser)
    subparsers = parser.add_subparsers(dest="command")

    def _sub(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(
            name, help=help_text, parents=[], add_help=True
        )
        # Re-add the global flags on each subparser so they can appear after
        # the subcommand (e.g. ``instances --dry-run``).
        _add_global_flags(sub)
        return sub

    sub_regions = _sub("regions", "List available regions.")
    sub_regions.set_defaults(func=cmd_regions)

    sub_products = _sub("products", "List available products/plans.")
    sub_products.set_defaults(func=cmd_products)

    sub_os = _sub("os", "List available operating system images.")
    sub_os.set_defaults(func=cmd_os)

    sub_ssh = _sub("ssh-keys", "List registered SSH keys.")
    sub_ssh.set_defaults(func=cmd_ssh_keys)

    sub_instances = _sub("instances", "List existing instances.")
    sub_instances.set_defaults(func=cmd_instances)

    sub_provision = _sub("provision", "Create a new instance.")
    sub_provision.add_argument("--region", required=True, help="Region id.")
    sub_provision.add_argument("--product", required=True, help="Product/plan id.")
    sub_provision.add_argument("--os", default=None, help="Operating system id.")
    sub_provision.add_argument(
        "--application", default=None, help="Application/marketplace image id."
    )
    sub_provision.add_argument("--hostname", default=None, help="Instance hostname.")
    sub_provision.add_argument(
        "--ssh-key",
        action="append",
        default=None,
        help="SSH key id to attach (repeatable).",
    )
    sub_provision.add_argument("--label", default=None, help="Instance label.")
    sub_provision.add_argument(
        "--wait",
        action="store_true",
        help="Poll the new instance until it is ready before returning.",
    )
    _add_wait_flags(sub_provision)
    sub_provision.set_defaults(func=cmd_provision)

    sub_wait = _sub("wait", "Poll an instance until it is ready.")
    sub_wait.add_argument("instance_id", help="Instance id to poll.")
    _add_wait_flags(sub_wait)
    sub_wait.set_defaults(func=cmd_wait)

    sub_destroy = _sub("destroy", "Destroy an instance.")
    sub_destroy.add_argument("instance_id", help="Instance id to destroy.")
    sub_destroy.set_defaults(func=cmd_destroy)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    command = getattr(args, "command", None)
    if command is None:
        emit_json(
            {
                "ok": False,
                "command": "cloudzy",
                "error": {
                    "code": "usage_error",
                    "message": "a subcommand is required",
                    "retryable": False,
                    "details": {"usage": parser.format_usage().strip()},
                },
            }
        )
        return 2

    # Global dry-run: never contact the network or require a token.
    if getattr(args, "dry_run", False):
        envelope = _dry_run(command, _dry_run_plan(args))
        emit_json(envelope)
        return _exit_code(envelope)

    try:
        envelope = args.func(args)
    except Exception as exc:  # normalized error envelope; one JSON doc out
        envelope = _error_envelope(command, exc)

    emit_json(envelope)
    return _exit_code(envelope)


if __name__ == "__main__":
    raise SystemExit(main())
