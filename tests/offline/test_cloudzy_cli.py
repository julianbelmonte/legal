"""Offline tests for the Cloudzy CLI (no network, no token)."""

from __future__ import annotations

import json

import pytest

from deploy import cloudzy_cli


def _run(capsys, argv):
    code = cloudzy_cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, f"expected exactly one JSON document, got: {out!r}"
    return code, json.loads(out[0])


@pytest.mark.parametrize(
    "command",
    ["regions", "products", "os", "ssh-keys", "instances"],
)
def test_read_dry_run_no_token(capsys, monkeypatch, command):
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    code, doc = _run(capsys, [command, "--dry-run"])
    assert code == 0
    assert doc["ok"] is True
    assert doc["dry_run"] is True
    assert doc["command"] == command
    assert doc["plan"]["method"] == "read"


def test_provision_dry_run_echoes_plan_without_network(capsys, monkeypatch):
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    code, doc = _run(
        capsys,
        [
            "provision",
            "--region",
            "us-east",
            "--product",
            "vps-1",
            "--ssh-key",
            "k1",
            "--wait",
            "--dry-run",
        ],
    )
    assert code == 0
    assert doc["dry_run"] is True
    assert doc["plan"]["method"] == "create"
    assert doc["plan"]["request"]["region"] == "us-east"
    assert doc["plan"]["wait"] is True


def test_destroy_dry_run_no_network(capsys, monkeypatch):
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    code, doc = _run(capsys, ["destroy", "inst-123", "--dry-run"])
    assert code == 0
    assert doc["plan"]["method"] == "delete"
    assert doc["plan"]["instance_id"] == "inst-123"


def test_read_without_token_returns_error_envelope(capsys, monkeypatch, tmp_path):
    # No env var AND no deploy.env token -> a real read must error.
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGAL_DEPLOY_ENV_FILE", str(tmp_path / "absent.env"))
    code, doc = _run(capsys, ["regions"])
    assert code == 1
    assert doc["ok"] is False
    assert doc["error"]["code"] == "cloudzy_error"


def test_token_resolved_from_deploy_env_when_env_unset(capsys, monkeypatch, tmp_path):
    # When --token and CLOUDZY_API_TOKEN are absent, cloudzy_cli falls back to
    # the gitignored deploy.env (same source the orchestrator uses).
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    env_file = tmp_path / "deploy.env"
    env_file.write_text("CLOUDZY_API_TOKEN=tok-from-file\n", encoding="utf-8")
    monkeypatch.setenv("LEGAL_DEPLOY_ENV_FILE", str(env_file))

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, token=None):
            captured["token"] = token

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_regions(self):
            return ["r1"]

    monkeypatch.setattr(cloudzy_cli, "CloudzyClient", _FakeClient)
    code, doc = _run(capsys, ["regions"])
    assert code == 0
    assert captured["token"] == "tok-from-file"


def test_no_subcommand_is_usage_error(capsys):
    code, doc = _run(capsys, [])
    assert code == 2
    assert doc["ok"] is False
    assert doc["error"]["code"] == "usage_error"


def test_token_never_appears_in_output(capsys, monkeypatch):
    secret = "super-secret-token-value"
    monkeypatch.setenv("CLOUDZY_API_TOKEN", secret)
    # Dry-run must not contact the network and must not echo the token.
    cloudzy_cli.main(["instances", "--dry-run"])
    out = capsys.readouterr().out
    assert secret not in out
