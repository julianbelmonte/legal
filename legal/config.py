"""Runtime configuration for the standalone legal CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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
    """Return the Capsolver API key from env or local config."""

    value = _non_empty(os.environ.get("LEGAL_CAPSOLVER_API_KEY"))
    if value is None:
        value = _non_empty(_configured_value("CAPSOLVER_API_KEY"))
    if value is None:
        raise RuntimeError(
            "Capsolver API key is not configured. Set LEGAL_CAPSOLVER_API_KEY "
            "or run apps/legal/scripts/bootstrap.py to create apps/legal/local_config.py."
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
            "apps/legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_BIN."
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
            "apps/legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_PROFILES_DIR."
        )
    return path


def pick_profile() -> Path:
    """Return a deterministic BotBrowser .enc profile path."""

    profiles_dir = botbrowser_profiles_dir()
    profiles = sorted(profiles_dir.glob("*.enc"))
    if not profiles:
        raise RuntimeError(
            f"No BotBrowser .enc profiles found in {profiles_dir}. Run "
            "apps/legal/scripts/bootstrap.py or set LEGAL_BOTBROWSER_PROFILES_DIR."
        )
    return profiles[0]


__all__ = [
    "capsolver_api_key",
    "botbrowser_bin",
    "botbrowser_profiles_dir",
    "pick_profile",
]
