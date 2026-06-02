"""Deploy secret loader for Cloudzy and ngrok credentials.

This module groups the local deploy credentials needed by the deployment
automation and exposes them through a single, redaction-safe surface. It reads:

- ``CLOUDZY_API_TOKEN`` (and any other ``KEY=VALUE`` deploy keys) from a deploy
  env file, by default ``~/.config/legal-agent/deploy.env`` and overridable via
  the ``LEGAL_DEPLOY_ENV_FILE`` environment variable.
- the ngrok ``authtoken`` from the ngrok config (default
  ``~/.config/ngrok/ngrok.yml``, overridable via ``NGROK_CONFIG_FILE``) or from
  the ``NGROK_AUTHTOKEN`` environment variable.

Precedence for the ngrok authtoken: ``NGROK_AUTHTOKEN`` in the environment wins;
the ngrok config file is the fallback when the env var is unset.

Safety: this module never prints, logs, or returns raw secret values. Every
diagnostic and JSON-shaped report runs values through :func:`redact_secret`.
Raw values are reachable only by explicitly reading the dataclass fields.

This module is standalone deploy tooling and does not import from the legal
pipeline's source-access internals.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Deploy env file location.
DEPLOY_ENV_FILE_ENV_VAR = "LEGAL_DEPLOY_ENV_FILE"
DEFAULT_DEPLOY_ENV_FILE = Path.home() / ".config" / "legal-agent" / "deploy.env"

#: ngrok config location and env override.
NGROK_CONFIG_FILE_ENV_VAR = "NGROK_CONFIG_FILE"
DEFAULT_NGROK_CONFIG_FILE = Path.home() / ".config" / "ngrok" / "ngrok.yml"
NGROK_AUTHTOKEN_ENV_VAR = "NGROK_AUTHTOKEN"

#: Required deploy keys read from the deploy env file.
CLOUDZY_TOKEN_KEY = "CLOUDZY_API_TOKEN"

#: How many leading characters of a secret are safe to reveal in a preview.
REDACT_PREFIX_LEN = 3
#: A value shorter than this is redacted entirely (revealing a prefix of a tiny
#: secret would leak too much of it).
REDACT_MIN_LEN = 6


class DeploySecretError(RuntimeError):
    """Raised when a required deploy secret is missing or unreadable."""


def redact_secret(value: str | None) -> str:
    """Return a redaction-safe preview of ``value``.

    Keeps the first :data:`REDACT_PREFIX_LEN` characters and masks the rest, so
    the masked form is recognizable without exposing the secret. Empty or very
    short values are masked entirely.

    Examples::

        redact_secret("abcdef123456") -> "abc*********"
        redact_secret("ab")           -> "**"
        redact_secret("")             -> "(empty)"
        redact_secret(None)           -> "(unset)"
    """
    if value is None:
        return "(unset)"
    if value == "":
        return "(empty)"
    if len(value) < REDACT_MIN_LEN:
        return "*" * len(value)
    prefix = value[:REDACT_PREFIX_LEN]
    return prefix + "*" * (len(value) - REDACT_PREFIX_LEN)


@dataclass
class DeploySecrets:
    """Resolved deploy secrets plus their (sanitized) provenance.

    Raw values live in ``cloudzy_api_token`` / ``ngrok_authtoken`` /
    ``extra``; never serialize them directly. Use :meth:`diagnostics` for any
    reportable / JSON output.
    """

    cloudzy_api_token: str | None = None
    ngrok_authtoken: str | None = None
    #: Other ``KEY=VALUE`` deploy keys found in the env file (raw values).
    extra: dict[str, str] = None  # type: ignore[assignment]
    #: Source path/origin per secret name, for diagnostics.
    sources: dict[str, str] = None  # type: ignore[assignment]
    #: Non-fatal warnings (e.g. loose file permissions).
    warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = {}
        if self.sources is None:
            self.sources = {}
        if self.warnings is None:
            self.warnings = []

    def require(self, *names: str) -> None:
        """Raise :class:`DeploySecretError` if any named secret is absent.

        Recognized names: ``CLOUDZY_API_TOKEN``, ``NGROK_AUTHTOKEN``. The error
        names the missing secret and where to provide it.
        """
        missing: list[str] = []
        for name in names:
            if name == CLOUDZY_TOKEN_KEY and not self.cloudzy_api_token:
                missing.append(
                    f"{CLOUDZY_TOKEN_KEY} (set it in the deploy env file, default "
                    f"{DEFAULT_DEPLOY_ENV_FILE}, or override the path with "
                    f"{DEPLOY_ENV_FILE_ENV_VAR})"
                )
            elif name == NGROK_AUTHTOKEN_ENV_VAR and not self.ngrok_authtoken:
                missing.append(
                    f"{NGROK_AUTHTOKEN_ENV_VAR} (set the {NGROK_AUTHTOKEN_ENV_VAR} "
                    f"env var or an 'authtoken' field in the ngrok config, default "
                    f"{DEFAULT_NGROK_CONFIG_FILE})"
                )
            elif name not in (CLOUDZY_TOKEN_KEY, NGROK_AUTHTOKEN_ENV_VAR):
                if not self.extra.get(name):
                    missing.append(f"{name} (set it in the deploy env file)")
        if missing:
            raise DeploySecretError(
                "missing required deploy secret(s): " + "; ".join(missing)
            )

    def diagnostics(self) -> dict[str, Any]:
        """Return a sanitized, JSON-safe report of which secrets are present.

        Every secret value is passed through :func:`redact_secret`; raw values
        never appear in the output.
        """
        secrets: dict[str, Any] = {
            CLOUDZY_TOKEN_KEY: {
                "present": bool(self.cloudzy_api_token),
                "preview": redact_secret(self.cloudzy_api_token),
                "source": self.sources.get(CLOUDZY_TOKEN_KEY),
            },
            NGROK_AUTHTOKEN_ENV_VAR: {
                "present": bool(self.ngrok_authtoken),
                "preview": redact_secret(self.ngrok_authtoken),
                "source": self.sources.get(NGROK_AUTHTOKEN_ENV_VAR),
            },
        }
        for key, value in self.extra.items():
            secrets[key] = {
                "present": bool(value),
                "preview": redact_secret(value),
                "source": self.sources.get(key),
            }
        return {"secrets": secrets, "warnings": list(self.warnings)}


def _check_permissions(path: Path, warnings: list[str]) -> None:
    """Append a warning if ``path`` is group- or world-readable.

    Deploy secret files are expected to be ``0600``.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(
            f"{path} has loose permissions {oct(stat.S_IMODE(mode))}; "
            "deploy secret files should be 0600 (owner read/write only)"
        )


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` deploy env file into a dict.

    Skips blank lines and ``#`` comments, tolerates an optional ``export``
    prefix, and strips matching surrounding quotes from values.
    """
    result: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _resolve_deploy_env_file(path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(DEPLOY_ENV_FILE_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_DEPLOY_ENV_FILE


def _resolve_ngrok_config_file(path: str | os.PathLike[str] | None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(NGROK_CONFIG_FILE_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_NGROK_CONFIG_FILE


def _read_ngrok_authtoken(path: Path) -> str | None:
    """Read the ``authtoken`` field from an ngrok YAML config.

    Uses PyYAML when importable; otherwise falls back to a minimal single-line
    parse so this module stays dependency-light. Returns ``None`` when the file
    is absent or has no authtoken.
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            token = data.get("authtoken")
            if isinstance(token, str) and token.strip():
                return token.strip()
            # Newer ngrok configs may nest under "agent".
            agent = data.get("agent")
            if isinstance(agent, dict):
                token = agent.get("authtoken")
                if isinstance(token, str) and token.strip():
                    return token.strip()
            return None
    except ImportError:
        pass
    # Minimal fallback: find a top-level-ish "authtoken: <value>" line.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("authtoken:"):
            value = line.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                return value
    return None


def load_deploy_secrets(
    *,
    deploy_env_file: str | os.PathLike[str] | None = None,
    ngrok_config_file: str | os.PathLike[str] | None = None,
    require: bool = False,
) -> DeploySecrets:
    """Load deploy secrets from the env file and ngrok config.

    :param deploy_env_file: Override the deploy env file path. Falls back to
        ``LEGAL_DEPLOY_ENV_FILE`` then :data:`DEFAULT_DEPLOY_ENV_FILE`.
    :param ngrok_config_file: Override the ngrok config path. Falls back to
        ``NGROK_CONFIG_FILE`` then :data:`DEFAULT_NGROK_CONFIG_FILE`.
    :param require: When true, raise :class:`DeploySecretError` if either the
        Cloudzy token or the ngrok authtoken is missing.

    The ngrok authtoken resolves from ``NGROK_AUTHTOKEN`` first, then the ngrok
    config file. Loose file permissions are recorded as warnings, not errors.
    """
    secrets = DeploySecrets()

    env_path = _resolve_deploy_env_file(deploy_env_file)
    if env_path.exists():
        _check_permissions(env_path, secrets.warnings)
        try:
            parsed = _parse_env_file(env_path)
        except OSError as exc:
            raise DeploySecretError(
                f"could not read deploy env file {env_path}: {exc}"
            ) from exc
        for key, value in parsed.items():
            if key == CLOUDZY_TOKEN_KEY:
                secrets.cloudzy_api_token = value
                secrets.sources[CLOUDZY_TOKEN_KEY] = str(env_path)
            else:
                secrets.extra[key] = value
                secrets.sources[key] = str(env_path)
    else:
        secrets.warnings.append(f"deploy env file not found at {env_path}")

    # Env var for the Cloudzy token takes precedence over the file value.
    env_token = os.environ.get(CLOUDZY_TOKEN_KEY)
    if env_token:
        secrets.cloudzy_api_token = env_token
        secrets.sources[CLOUDZY_TOKEN_KEY] = f"env:{CLOUDZY_TOKEN_KEY}"

    # ngrok authtoken: env var wins, config file is the fallback.
    env_ngrok = os.environ.get(NGROK_AUTHTOKEN_ENV_VAR)
    if env_ngrok:
        secrets.ngrok_authtoken = env_ngrok
        secrets.sources[NGROK_AUTHTOKEN_ENV_VAR] = f"env:{NGROK_AUTHTOKEN_ENV_VAR}"
    else:
        ngrok_path = _resolve_ngrok_config_file(ngrok_config_file)
        if ngrok_path.exists():
            _check_permissions(ngrok_path, secrets.warnings)
            try:
                token = _read_ngrok_authtoken(ngrok_path)
            except OSError as exc:
                raise DeploySecretError(
                    f"could not read ngrok config {ngrok_path}: {exc}"
                ) from exc
            if token:
                secrets.ngrok_authtoken = token
                secrets.sources[NGROK_AUTHTOKEN_ENV_VAR] = str(ngrok_path)
        else:
            secrets.warnings.append(f"ngrok config not found at {ngrok_path}")

    if require:
        secrets.require(CLOUDZY_TOKEN_KEY, NGROK_AUTHTOKEN_ENV_VAR)

    return secrets
