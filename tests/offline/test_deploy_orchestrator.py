"""Offline tests for the deploy orchestrator (no network, no SSH, no token)."""

from __future__ import annotations

import json

import pytest

from legal_deploy import deploy


SECRET_TOKEN = "cz_supersecret_token_value_1234567890"


def _run(capsys, argv):
    code = deploy.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, f"expected exactly one JSON document, got: {out!r}"
    return code, json.loads(out[0])


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch):
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)


def test_dry_run_json_emits_one_plan_document(capsys):
    code, doc = _run(capsys, ["deploy", "--dry-run", "--json"])
    assert code == 0
    assert doc["ok"] is True
    assert doc["dry_run"] is True
    assert doc["command"] == "deploy"
    plan = doc["plan"]
    assert plan["action"] == "deploy"
    assert plan["app_dir"] == deploy.DEFAULT_APP_DIR
    assert plan["service_user"] == deploy.DEFAULT_SERVICE_USER
    assert isinstance(plan["steps"], list) and plan["steps"]
    assert plan["systemd_units"]
    assert "remote_command_summary" in plan


def test_bare_dry_run_json_defaults_to_deploy(capsys):
    # No subcommand: should default to deploy and still print one plan doc.
    code, doc = _run(capsys, ["--dry-run", "--json"])
    assert code == 0
    assert doc["plan"]["action"] == "deploy"


def test_dry_run_destroy_plan(capsys):
    code, doc = _run(capsys, ["destroy", "--dry-run", "--json"])
    assert code == 0
    assert doc["plan"]["action"] == "destroy"
    assert "steps" in doc["plan"]


def test_dry_run_does_not_leak_secrets(capsys, monkeypatch, tmp_path):
    # Even when a real token is present in the env, dry-run must not print it.
    monkeypatch.setenv("CLOUDZY_API_TOKEN", SECRET_TOKEN)
    monkeypatch.setenv("NGROK_AUTHTOKEN", "ngrok_secret_authtoken_value")
    code = deploy.main(["deploy", "--dry-run", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert SECRET_TOKEN not in out
    assert "ngrok_secret_authtoken_value" not in out


def test_dry_run_needs_no_token(capsys):
    # No token configured anywhere: dry-run still succeeds.
    code, doc = _run(capsys, ["deploy", "--dry-run", "--json"])
    assert code == 0
    assert doc["ok"] is True


def test_plan_respects_custom_app_dir(capsys):
    code, doc = _run(
        capsys,
        ["deploy", "--dry-run", "--json", "--app-dir", "/srv/legal", "--service-user", "svc"],
    )
    assert code == 0
    assert doc["plan"]["app_dir"] == "/srv/legal"
    assert doc["plan"]["service_user"] == "svc"
    assert doc["plan"]["remote_env_file"] == "/srv/legal/.env"


def test_state_roundtrip(tmp_path):
    state_file = tmp_path / "state.json"
    deploy.save_state({"instance_id": "abc", "ip": "1.2.3.4"}, state_file)
    loaded = deploy.load_state(state_file)
    assert loaded["instance_id"] == "abc"
    assert loaded["ip"] == "1.2.3.4"


def test_instance_ip_extraction():
    assert deploy._instance_ip({"mainIp": "10.0.0.1"}) == "10.0.0.1"
    assert deploy._instance_ip({"networks": [{"public_ip": "2.2.2.2"}]}) == "2.2.2.2"
    assert deploy._instance_ip({"nope": 1}) is None


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--help"])
    assert exc.value.code == 0
