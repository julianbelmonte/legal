"""Runtime configuration for the standalone legal CLI."""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any

try:
    from legal import secret as _secret
except ImportError:
    _secret = None

try:
    from legal import local_config as _local_config
except ImportError:
    _local_config = None


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent


def _configured_value(name: str) -> Any:
    if _local_config is None:
        return None
    return getattr(_local_config, name, None)


def _secret_value(name: str) -> Any:
    """Resolve a secret named ``name`` through the standard chain.

    Order: environment (``LEGAL_<NAME>``, plus the historical
    ``LEGAL_CAPSOLVER_API_KEY``) → ``legal.secret`` module → legacy
    ``legal.local_config`` module → ``None``.
    """

    env_names = [f"LEGAL_{name}"]
    if name == "CAPSOLVER_API_KEY" and "LEGAL_CAPSOLVER_API_KEY" not in env_names:
        env_names.append("LEGAL_CAPSOLVER_API_KEY")
    for env_name in env_names:
        value = _non_empty(os.environ.get(env_name))
        if value is not None:
            return value

    if _secret is not None:
        value = _non_empty(getattr(_secret, name, None))
        if value is not None:
            return value

    return _non_empty(_configured_value(name))


def _non_empty(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _config_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _HERE / path
    return path.resolve(strict=False)


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def capsolver_api_key() -> str:
    """Return the Capsolver API key from env, secret.py, or local config."""

    value = _secret_value("CAPSOLVER_API_KEY")
    if value is None:
        raise RuntimeError(
            "Capsolver API key is not configured. Set LEGAL_CAPSOLVER_API_KEY "
            "or add CAPSOLVER_API_KEY to legal/secret.py "
            "(copy legal/secret.example.py)."
        )
    return str(value)


def floxy_user() -> str:
    """Return the Floxy proxy username from env, secret.py, or local config."""

    value = _secret_value("FLOXY_USER")
    if value is None:
        raise RuntimeError(
            "Floxy user is not configured. Set LEGAL_FLOXY_USER "
            "or add FLOXY_USER to legal/secret.py "
            "(copy legal/secret.example.py)."
        )
    return str(value)


def floxy_pass() -> str:
    """Return the Floxy proxy password from env, secret.py, or local config."""

    value = _secret_value("FLOXY_PASS")
    if value is None:
        raise RuntimeError(
            "Floxy pass is not configured. Set LEGAL_FLOXY_PASS "
            "or add FLOXY_PASS to legal/secret.py "
            "(copy legal/secret.example.py)."
        )
    return str(value)


def anyip_user() -> str:
    """Return the AnyIP proxy username from env, secret.py, or local config."""

    value = _secret_value("ANYIP_USER")
    if value is None:
        raise RuntimeError(
            "AnyIP user is not configured. Set LEGAL_ANYIP_USER "
            "or add ANYIP_USER to legal/secret.py "
            "(copy legal/secret.example.py)."
        )
    return str(value)


def anyip_pass() -> str:
    """Return the AnyIP proxy password from env, secret.py, or local config."""

    value = _secret_value("ANYIP_PASS")
    if value is None:
        raise RuntimeError(
            "AnyIP pass is not configured. Set LEGAL_ANYIP_PASS "
            "or add ANYIP_PASS to legal/secret.py "
            "(copy legal/secret.example.py)."
        )
    return str(value)


def botbrowser_bin() -> Path:
    """Return the BotBrowser Chromium executable path."""

    value = _non_empty(os.environ.get("LEGAL_BOTBROWSER_BIN"))
    if value is None:
        value = _non_empty(_configured_value("BOTBROWSER_BIN"))
    if value is not None:
        return _config_path(value)

    path = _first_existing(
        [
            _HERE / "vendor" / "botbrowser" / "chromium-browser",
            Path("/opt/chromium.org/chromium/chromium-browser"),
        ]
    )
    if path is None:
        raise RuntimeError(
            "BotBrowser Chromium executable was not found. Run "
            "legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_BIN."
        )
    return path


def botbrowser_profiles_dir() -> Path:
    """Return the directory containing BotBrowser .enc profiles."""

    value = _non_empty(os.environ.get("LEGAL_BOTBROWSER_PROFILES_DIR"))
    if value is None:
        value = _non_empty(_configured_value("BOTBROWSER_PROFILES_DIR"))
    if value is not None:
        return _config_path(value)

    path = _first_existing(
        [
            _HERE / "vendor" / "profiles",
            _REPO_ROOT / "drone" / "engines" / "botbrowser" / "profiles",
        ]
    )
    if path is None:
        raise RuntimeError(
            "BotBrowser profiles directory was not found. Run "
            "legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_PROFILES_DIR."
        )
    return path


def pick_profile() -> Path:
    """Return a BotBrowser .enc profile path.

    Rotates randomly across all available ``*.enc`` profiles. If
    ``settings.botbrowser_profile`` (env ``LEGAL_BOTBROWSER_PROFILE``) is set,
    the matching profile is pinned for reproducible runs.
    """

    from legal import settings as _settings

    profiles_dir = botbrowser_profiles_dir()
    profiles = sorted(profiles_dir.glob("*.enc"))
    if not profiles:
        raise RuntimeError(
            f"No BotBrowser .enc profiles found in {profiles_dir}. Run "
            "legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_PROFILES_DIR."
        )

    pinned = _non_empty(_settings.get_settings().botbrowser_profile)
    if pinned is not None:
        for profile in profiles:
            if profile.name == pinned or profile.stem == pinned:
                return profile
        available = ", ".join(p.name for p in profiles)
        raise RuntimeError(
            f"Pinned BotBrowser profile {pinned!r} "
            f"(LEGAL_BOTBROWSER_PROFILE) was not found in {profiles_dir}. "
            f"Available profiles: {available}."
        )

    return random.choice(profiles)


__all__ = [
    "capsolver_api_key",
    "floxy_user",
    "floxy_pass",
    "anyip_user",
    "anyip_pass",
    "botbrowser_bin",
    "botbrowser_profiles_dir",
    "pick_profile",
]
