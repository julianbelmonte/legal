"""Offline tests for the deploy orchestrator (no network, no SSH, no token)."""

from __future__ import annotations

import json

import pytest

from deploy import deploy


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


def test_state_file_written_with_restrictive_perms(tmp_path):
    import stat

    state_file = tmp_path / "state.json"
    path = deploy.save_state({"instance_id": "abc"}, state_file)
    # save_state chmods the file to 0600 (owner read/write only).
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    # No group/other access bits are set.
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_missing_state_file_loads_empty(tmp_path):
    assert deploy.load_state(tmp_path / "nope.json") == {}


def test_corrupt_state_file_loads_empty(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{ not valid json", encoding="utf-8")
    assert deploy.load_state(state_file) == {}


def test_instance_ip_extraction():
    assert deploy._instance_ip({"mainIp": "10.0.0.1"}) == "10.0.0.1"
    assert deploy._instance_ip({"networks": [{"public_ip": "2.2.2.2"}]}) == "2.2.2.2"
    assert deploy._instance_ip({"nope": 1}) is None


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.main(["--help"])
    assert exc.value.code == 0


# -- domain / Caddy plan + DNS automation -----------------------------------


def test_deploy_plan_is_domain_caddy_not_ngrok(capsys):
    code, doc = _run(capsys, ["deploy", "--dry-run", "--json"])
    assert code == 0
    plan = doc["plan"]
    assert plan["domain"] == deploy.DEFAULT_DOMAIN
    assert plan["public_url"] == f"https://{deploy.DEFAULT_DOMAIN}"
    # Caddy fronts the app; the only rendered systemd unit is the app service.
    assert plan["systemd_units"] == ["legal-api.service"]
    blob = " ".join(plan["steps"]) + " ".join(plan["remote_command_summary"])
    assert "caddy" in blob.lower()
    assert "ngrok" not in blob.lower()
    assert plan["dns_repoint"] is False  # reuse deploy: no DNS repoint


def test_fresh_plan_includes_namecheap_dns_steps(capsys):
    code, doc = _run(capsys, ["deploy", "--fresh", "--dry-run", "--json"])
    assert code == 0
    plan = doc["plan"]
    assert plan["fresh"] is True
    assert plan["dns_repoint"] is True
    steps = " ".join(plan["steps"]).lower()
    assert "namecheap" in steps and "a record" in steps and "dns" in steps


def test_fresh_no_dns_skips_repoint(capsys):
    code, doc = _run(capsys, ["deploy", "--fresh", "--no-dns", "--dry-run", "--json"])
    assert code == 0
    assert doc["plan"]["dns_repoint"] is False


def test_custom_domain_flows_into_plan(capsys):
    code, doc = _run(
        capsys, ["deploy", "--dry-run", "--json", "--domain", "api.example.org"]
    )
    assert code == 0
    assert doc["plan"]["domain"] == "api.example.org"
    assert doc["plan"]["public_url"] == "https://api.example.org"


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_repoint_dns_success(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "dns-set-ip" in cmd:
            return _FakeProc(0, '{"ok": true, "records": [{"host": "mcp"}]}')
        return _FakeProc(0, "")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    res = deploy._repoint_dns("mcp.arglegal.live", "1.2.3.4")
    assert res["ok"] is True
    assert res["host"] == "mcp"
    assert res["ip"] == "1.2.3.4"
    # The session is logged in before any DNS mutation.
    assert any("login" in c for c in calls)
    # dns-set-ip targets the REGISTERED apex domain + host label (not the full
    # subdomain), and carries the new IP.
    set_ip = next(c for c in calls if "dns-set-ip" in c)
    assert "arglegal.live" in set_ip and "mcp.arglegal.live" not in set_ip
    assert "1.2.3.4" in set_ip
    assert "--host" in set_ip and "mcp" in set_ip


def test_repoint_dns_failure_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "dns-set-ip" in cmd:
            return _FakeProc(1, '{"ok": false}', "boom")
        return _FakeProc(0, "")

    monkeypatch.setattr(deploy.subprocess, "run", fake_run)
    with pytest.raises(deploy.DeployError):
        deploy._repoint_dns("mcp.arglegal.live", "1.2.3.4")


def test_wait_for_dns_returns_on_match(monkeypatch):
    monkeypatch.setattr(deploy, "_dns_resolves_to", lambda d, ip: True)
    assert deploy._wait_for_dns("x.example", "1.2.3.4", timeout=1.0, interval=0.01) is True


def test_wait_for_dns_times_out_is_nonfatal(monkeypatch):
    monkeypatch.setattr(deploy, "_dns_resolves_to", lambda d, ip: False)
    assert deploy._wait_for_dns("x.example", "1.2.3.4", timeout=0.05, interval=0.01) is False
