"""Offline tests for the VPS bootstrap script renderer.

These tests only inspect rendered strings: they never run any shell, install
anything, or read real secrets. They assert idempotence markers, the required
substrings the deploy step depends on, and that no real secret value leaks into
the rendered output.
"""

from __future__ import annotations

import pytest

import deploy.deploy as deploy_mod
from deploy.bootstrap import (
    DEFAULT_APP_PORT,
    VENDOR_BOOTSTRAP_REL,
    render_bootstrap_script,
    render_caddyfile,
    render_env_file,
    render_systemd_units,
)


def test_render_contains_required_substrings():
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    assert "uv" in script
    assert "caddy" in script  # Caddy fronts the app on the public domain
    assert "ngrok" not in script  # ngrok removed; Caddy/domain is the path
    assert "legal/scripts/bootstrap.py" in script
    assert VENDOR_BOOTSTRAP_REL in script


def test_render_has_idempotence_markers():
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    # Guards that make the script safe to re-run.
    assert "command -v uv" in script
    assert "command -v caddy" in script
    assert "id -u" in script
    assert "mkdir -p" in script
    assert "systemctl enable --now" in script
    assert "set -euo pipefail" in script


def test_render_configures_caddy_for_domain():
    script = render_bootstrap_script(
        app_dir="/opt/legal", service_user="legal", domain="mcp.arglegal.live"
    )
    assert "/etc/caddy/Caddyfile" in script
    assert "mcp.arglegal.live" in script
    assert "enable --now caddy" in script


def test_render_restarts_app_so_redeploys_pick_up_new_env():
    # `enable --now` won't restart a running unit; a redeploy must restart the
    # app so an updated env file / synced code takes effect.
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    assert "systemctl restart legal-api.service" in script


def test_sync_repo_does_not_exclude_legal_vendor(monkeypatch):
    # legal/vendor holds the BotBrowser binary + .enc profiles; excluding it from
    # the rsync left the VPS with no browser, so CSJN/PTN/PJN/SCBA were dead while
    # direct sources (SAIJ) worked. The sync must carry vendor onto the box.
    captured: dict[str, list[str]] = {}

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(deploy_mod.subprocess, "run", _fake_run)
    deploy_mod._sync_repo("1.2.3.4", user="legal", app_dir="/opt/legal", key_path=None)

    cmd = captured["cmd"]
    excludes = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--exclude"]
    assert "legal/vendor" not in excludes
    assert ".git" in excludes  # other excludes still intact


def test_render_sets_chrome_sandbox_setuid_root():
    # Chromium's setuid sandbox must be root:root 4755 or the browser cannot
    # launch on a VPS that restricts unprivileged user namespaces, taking every
    # browser-backed source (CSJN/PTN/PJN/SCBA) down.
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    assert "$APP_DIR/legal/vendor/botbrowser/chrome-sandbox" in script
    assert "chmod 4755" in script
    assert "chown root:root" in script


def test_render_gates_deploy_on_browser_launch():
    # The deploy must fail loudly when the browser cannot launch instead of
    # silently shipping a half-dead connector (the failure the smoke missed).
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    assert "from legal.browser import BotBrowser" in script
    assert "Aborting deploy" in script
    assert "exit 1" in script
    # but allow an explicit opt-out for API-only deploys
    assert "LEGAL_DEPLOY_SKIP_BROWSER_CHECK" in script


def test_render_references_app_dir_and_user():
    script = render_bootstrap_script(app_dir="/srv/app", service_user="webby")
    assert "/srv/app" in script
    assert "webby" in script
    # vendor helper is referenced under the app dir
    assert "/srv/app/legal/scripts/bootstrap.py" in script


def test_render_rejects_relative_app_dir():
    with pytest.raises(ValueError):
        render_bootstrap_script(app_dir="opt/legal", service_user="legal")


def test_no_real_secret_leaks_into_script():
    # The bootstrap script must not bake any secret value; it only references an
    # env file written separately at deploy time.
    script = render_bootstrap_script(app_dir="/opt/legal", service_user="legal")
    for needle in ("CAPSOLVER_API_KEY=", "FLOXY_PASS=", "NGROK_AUTHTOKEN=", "authtoken:"):
        assert needle not in script


def test_systemd_units_only_the_app():
    units = render_systemd_units(
        app_dir="/opt/legal",
        service_user="legal",
        app_port=DEFAULT_APP_PORT,
        app_env_file="/opt/legal/.env",
    )
    # Caddy fronts the app via a Caddyfile (not a rendered systemd unit here), so
    # the only rendered unit is the app service.
    assert list(units) == ["legal-api.service"]
    app_text = units["legal-api.service"]
    assert "uvicorn api.main:app" in app_text
    assert f"--port {DEFAULT_APP_PORT}" in app_text


def test_caddyfile_serves_mcp_at_domain_root():
    caddyfile = render_caddyfile("mcp.arglegal.live", DEFAULT_APP_PORT)
    assert "mcp.arglegal.live {" in caddyfile
    assert "rewrite @root /mcp/" in caddyfile
    assert f"reverse_proxy 127.0.0.1:{DEFAULT_APP_PORT}" in caddyfile


def test_env_file_writer_is_restrictive_and_quotes_values():
    snippet = render_env_file(
        {"CAPSOLVER_API_KEY": "deadbeef"},
        path="/opt/legal/.env",
        owner="legal",
    )
    assert "chmod 600 /opt/legal/.env" in snippet or "chmod '600'" in snippet
    assert "chmod" in snippet
    assert "CAPSOLVER_API_KEY=deadbeef" in snippet
