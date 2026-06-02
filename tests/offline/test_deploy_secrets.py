"""Offline tests for the deploy secret loader.

These tests build fake fixture files in ``tmp_path`` with dummy values. They
never read the real deploy.env / ngrok.yml and never assert on real tokens.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from legal_deploy.secrets import (
    CLOUDZY_TOKEN_KEY,
    NGROK_AUTHTOKEN_ENV_VAR,
    DeploySecretError,
    load_deploy_secrets,
    redact_secret,
)


def test_redact_keeps_short_prefix_and_masks_rest():
    redacted = redact_secret("abcdef123456")
    assert redacted.startswith("abc")
    assert "123456" not in redacted
    assert len(redacted) == len("abcdef123456")


def test_redact_handles_empty_short_and_none():
    assert redact_secret(None) == "(unset)"
    assert redact_secret("") == "(empty)"
    # A tiny value is fully masked, never partially revealed.
    assert redact_secret("ab") == "**"
    assert set(redact_secret("ab")) == {"*"}


def _write_env(tmp_path, contents, mode=0o600):
    path = tmp_path / "deploy.env"
    path.write_text(contents, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _write_ngrok(tmp_path, token="dummytoken12345"):
    path = tmp_path / "ngrok.yml"
    path.write_text(f"version: 2\nauthtoken: {token}\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_loads_cloudzy_token_and_extra_keys(tmp_path, monkeypatch):
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    env_path = _write_env(
        tmp_path,
        "# comment\nCLOUDZY_API_TOKEN=dummy_cloudzy_value\n"
        "export OTHER_KEY=\"quoted value\"\n",
    )
    ngrok_path = _write_ngrok(tmp_path)

    secrets = load_deploy_secrets(
        deploy_env_file=env_path, ngrok_config_file=ngrok_path
    )
    assert secrets.cloudzy_api_token == "dummy_cloudzy_value"
    assert secrets.extra["OTHER_KEY"] == "quoted value"
    assert secrets.ngrok_authtoken == "dummytoken12345"
    assert secrets.sources[CLOUDZY_TOKEN_KEY] == str(env_path)
    assert secrets.sources[NGROK_AUTHTOKEN_ENV_VAR] == str(ngrok_path)


def test_env_var_overrides_file_for_cloudzy(tmp_path, monkeypatch):
    env_path = _write_env(tmp_path, "CLOUDZY_API_TOKEN=file_value\n")
    monkeypatch.setenv(CLOUDZY_TOKEN_KEY, "env_value")
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    secrets = load_deploy_secrets(
        deploy_env_file=env_path, ngrok_config_file=tmp_path / "missing.yml"
    )
    assert secrets.cloudzy_api_token == "env_value"
    assert secrets.sources[CLOUDZY_TOKEN_KEY] == f"env:{CLOUDZY_TOKEN_KEY}"


def test_ngrok_env_takes_precedence_over_config(tmp_path, monkeypatch):
    env_path = _write_env(tmp_path, "CLOUDZY_API_TOKEN=dummy\n")
    ngrok_path = _write_ngrok(tmp_path, token="configtoken99999")
    monkeypatch.setenv(NGROK_AUTHTOKEN_ENV_VAR, "envtoken88888")
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    secrets = load_deploy_secrets(
        deploy_env_file=env_path, ngrok_config_file=ngrok_path
    )
    assert secrets.ngrok_authtoken == "envtoken88888"
    assert secrets.sources[NGROK_AUTHTOKEN_ENV_VAR] == f"env:{NGROK_AUTHTOKEN_ENV_VAR}"


def test_loose_permissions_recorded_as_warning(tmp_path, monkeypatch):
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    env_path = _write_env(
        tmp_path, "CLOUDZY_API_TOKEN=dummy\n", mode=0o644
    )
    secrets = load_deploy_secrets(
        deploy_env_file=env_path, ngrok_config_file=tmp_path / "missing.yml"
    )
    assert any("loose permissions" in w for w in secrets.warnings)


def test_missing_required_secret_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    # No cloudzy token in the file; ngrok present.
    env_path = _write_env(tmp_path, "OTHER=x\n")
    ngrok_path = _write_ngrok(tmp_path)
    with pytest.raises(DeploySecretError) as exc:
        load_deploy_secrets(
            deploy_env_file=env_path,
            ngrok_config_file=ngrok_path,
            require=True,
        )
    assert CLOUDZY_TOKEN_KEY in str(exc.value)


def test_diagnostics_are_json_safe_and_redacted(tmp_path, monkeypatch):
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    env_path = _write_env(tmp_path, "CLOUDZY_API_TOKEN=supersecretvalue123\n")
    ngrok_path = _write_ngrok(tmp_path, token="ngroksecretvalue456")
    secrets = load_deploy_secrets(
        deploy_env_file=env_path, ngrok_config_file=ngrok_path
    )
    diag = secrets.diagnostics()
    serialized = json.dumps(diag)
    assert "supersecretvalue123" not in serialized
    assert "ngroksecretvalue456" not in serialized
    assert diag["secrets"][CLOUDZY_TOKEN_KEY]["present"] is True
    assert diag["secrets"][CLOUDZY_TOKEN_KEY]["preview"].startswith("sup")
    assert diag["secrets"][NGROK_AUTHTOKEN_ENV_VAR]["present"] is True


def test_lookup_via_env_override_path(tmp_path, monkeypatch):
    env_path = _write_env(tmp_path, "CLOUDZY_API_TOKEN=dummy_via_env_path\n")
    monkeypatch.setenv("LEGAL_DEPLOY_ENV_FILE", str(env_path))
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.setenv(NGROK_AUTHTOKEN_ENV_VAR, "ng")
    secrets = load_deploy_secrets(ngrok_config_file=tmp_path / "missing.yml")
    assert secrets.cloudzy_api_token == "dummy_via_env_path"


def test_missing_files_recorded_as_warnings_without_require(tmp_path, monkeypatch):
    monkeypatch.delenv(CLOUDZY_TOKEN_KEY, raising=False)
    monkeypatch.delenv(NGROK_AUTHTOKEN_ENV_VAR, raising=False)
    secrets = load_deploy_secrets(
        deploy_env_file=tmp_path / "nope.env",
        ngrok_config_file=tmp_path / "nope.yml",
    )
    assert secrets.cloudzy_api_token is None
    assert secrets.ngrok_authtoken is None
    assert any("not found" in w for w in secrets.warnings)
