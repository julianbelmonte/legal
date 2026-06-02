"""Offline tests for legal_deploy.smoke_codex (dry-run plan and redaction)."""

from __future__ import annotations

import json

import pytest

from legal_deploy import smoke_codex


SERVER_URL = "https://example.ngrok.app/mcp"
SECRET_TOKEN = "supersecret-bearer-token-abcdef123456"


@pytest.fixture(autouse=True)
def _isolate_token_sources(monkeypatch, tmp_path):
    """Isolate auto-mint inputs so tests never read a real deploy state file.

    Points the deploy state file at a non-existent path and clears the signing
    env vars, so unless a test opts in, no bearer token is auto-minted.
    """
    monkeypatch.setenv("LEGAL_DEPLOY_STATE_FILE", str(tmp_path / "no-state.json"))
    monkeypatch.delenv(smoke_codex.SIGNING_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(smoke_codex.ALLOWED_EMAILS_ENV_VAR, raising=False)
    monkeypatch.delenv(smoke_codex.ISSUER_ENV_VAR, raising=False)


# --- --help exits 0 ---------------------------------------------------------


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        smoke_codex.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "smoke_codex" in out


# --- dry-run plan shape (JSON) ----------------------------------------------


def test_dry_run_json_plan_shape(capsys):
    rc = smoke_codex.main(
        ["--dry-run", "--json", "--server-url", SERVER_URL, "--bearer-token", SECRET_TOKEN]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    plan = payload["plan"]
    # server-url echoed
    assert plan["server_url"] == SERVER_URL
    # the three smoke steps are present and in order
    step_ids = [s["id"] for s in plan["steps"]]
    assert step_ids == ["list_tools", "legal_sources", "legal_search"]
    # codex commands rendered
    cmd_steps = [c["step"] for c in plan["commands"]]
    assert cmd_steps == ["list_tools", "legal_sources", "legal_search"]


def test_dry_run_redacts_bearer_token(capsys):
    rc = smoke_codex.main(
        ["--dry-run", "--json", "--server-url", SERVER_URL, "--bearer-token", SECRET_TOKEN]
    )
    assert rc == 0
    raw = capsys.readouterr().out
    # The raw secret must never appear anywhere in the output.
    assert SECRET_TOKEN not in raw
    payload = json.loads(raw)
    plan = payload["plan"]
    assert plan["bearer_token_present"] is True
    assert plan["bearer_token"] == smoke_codex.redact_secret(SECRET_TOKEN)
    # The redacted token shows a short prefix but not the full secret.
    assert plan["bearer_token"].startswith(SECRET_TOKEN[:3])
    assert SECRET_TOKEN not in plan["temp_config_toml"]
    cfg_server = plan["temp_config"]["mcp_servers"]["legal"]
    # The token is NOT embedded in the codex config; it is referenced via an
    # env var, so the raw token never appears in the rendered config at all.
    assert cfg_server["bearer_token_env_var"] == smoke_codex.CODEX_BEARER_ENV_VAR
    assert SECRET_TOKEN not in json.dumps(plan["temp_config"])


def test_dry_run_token_from_env(monkeypatch, capsys):
    monkeypatch.setenv(smoke_codex.BEARER_TOKEN_ENV_VAR, SECRET_TOKEN)
    rc = smoke_codex.main(["--dry-run", "--json", "--server-url", SERVER_URL])
    assert rc == 0
    raw = capsys.readouterr().out
    assert SECRET_TOKEN not in raw
    payload = json.loads(raw)
    assert payload["plan"]["bearer_token_present"] is True


def test_dry_run_no_token_ok(monkeypatch, capsys):
    monkeypatch.delenv(smoke_codex.BEARER_TOKEN_ENV_VAR, raising=False)
    # ``--no-auto-mint`` disables minting from the deploy state file / signing
    # env, so with no token supplied the plan carries no bearer token.
    rc = smoke_codex.main(
        ["--dry-run", "--json", "--no-auto-mint", "--server-url", SERVER_URL]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    plan = payload["plan"]
    assert plan["bearer_token_present"] is False
    # No bearer_token key in the server config when none is configured.
    assert "bearer_token" not in plan["temp_config"]["mcp_servers"]["legal"]


def test_dry_run_auto_mints_from_signing_env(monkeypatch, capsys):
    """With signing env present and no explicit token, a token is auto-minted."""
    monkeypatch.delenv(smoke_codex.BEARER_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv(smoke_codex.SIGNING_KEY_ENV_VAR, "x" * 48)
    monkeypatch.setenv(smoke_codex.ALLOWED_EMAILS_ENV_VAR, "user@example.com")
    rc = smoke_codex.main(["--dry-run", "--json", "--server-url", SERVER_URL])
    assert rc == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    plan = payload["plan"]
    assert plan["bearer_token_present"] is True
    # The minted token is referenced via an env var, not embedded in the config.
    cfg_server = plan["temp_config"]["mcp_servers"]["legal"]
    assert cfg_server["bearer_token_env_var"] == smoke_codex.CODEX_BEARER_ENV_VAR
    # The raw minted JWT must not leak into the rendered config.
    assert "bearer_token" not in cfg_server


def test_dry_run_text_plan(monkeypatch, capsys):
    monkeypatch.delenv(smoke_codex.BEARER_TOKEN_ENV_VAR, raising=False)
    rc = smoke_codex.main(["--dry-run", "--server-url", SERVER_URL])
    assert rc == 0
    out = capsys.readouterr().out
    assert SERVER_URL in out
    assert "DRY RUN" in out
    assert "legal_sources" in out


def test_dry_run_url_from_env(monkeypatch, capsys):
    monkeypatch.setenv(smoke_codex.REMOTE_URL_ENV_VAR, SERVER_URL)
    monkeypatch.delenv(smoke_codex.BEARER_TOKEN_ENV_VAR, raising=False)
    rc = smoke_codex.main(["--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["server_url"] == SERVER_URL


def test_missing_server_url_is_usage_error(monkeypatch, capsys):
    monkeypatch.delenv(smoke_codex.REMOTE_URL_ENV_VAR, raising=False)
    rc = smoke_codex.main(["--dry-run", "--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"


# --- config building / redaction units --------------------------------------


def test_build_config_references_bearer_env_var():
    config = smoke_codex.build_codex_config(SERVER_URL, SECRET_TOKEN)
    server = config["mcp_servers"]["legal"]
    assert server["url"] == SERVER_URL
    # Codex reads the bearer token from an env var; the raw token is never
    # written into the config mapping.
    assert server["bearer_token_env_var"] == smoke_codex.CODEX_BEARER_ENV_VAR
    assert "bearer_token" not in server
    assert SECRET_TOKEN not in json.dumps(config)


def test_config_without_token_has_no_bearer_ref():
    config = smoke_codex.build_codex_config(SERVER_URL, None)
    server = config["mcp_servers"]["legal"]
    assert server["url"] == SERVER_URL
    assert "bearer_token_env_var" not in server
    assert "bearer_token" not in server


def test_render_toml_has_server_table():
    config = smoke_codex.build_codex_config(SERVER_URL, None)
    toml = smoke_codex.render_config_toml(config)
    assert "[mcp_servers.legal]" in toml
    assert f'url = "{SERVER_URL}"' in toml
