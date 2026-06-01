"""Bootstrap vendored browser assets for the standalone legal CLI."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


LEGAL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LEGAL_ROOT.parent.parent

VENDOR_DIR = LEGAL_ROOT / "vendor"
BOTBROWSER_DEST = VENDOR_DIR / "botbrowser"
PROFILES_DEST = VENDOR_DIR / "profiles"
LOCAL_CONFIG = LEGAL_ROOT / "local_config.py"
LOCAL_CONFIG_EXAMPLE = LEGAL_ROOT / "local_config.example.py"

DEFAULT_BOTBROWSER_SRC = Path("/opt/chromium.org/chromium")
DEFAULT_PROFILES_SRC = REPO_ROOT / "drone" / "engines" / "botbrowser" / "profiles"


@dataclass
class BootstrapSummary:
    botbrowser: str = "skipped"
    profiles_copied: int = 0
    profiles_skipped: int = 0
    local_config: str = "skipped"


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    return Path(value).expanduser().resolve(strict=False)


def _format_size(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size

    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _ensure_dir(path: Path, *, dry_run: bool) -> None:
    if path.exists():
        return
    if dry_run:
        print(f"would create {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def _copy_missing_tree(src: Path, dest: Path, *, dry_run: bool) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for item in src.rglob("*"):
        relpath = item.relative_to(src)
        target = dest / relpath

        if item.is_dir():
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
            continue

        if not item.is_file():
            continue

        if target.exists():
            skipped += 1
            continue

        copied += 1
        if dry_run:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)

    return copied, skipped


def _ensure_executable(path: Path, *, dry_run: bool) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode
    desired = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if desired == mode:
        return
    if dry_run:
        print(f"would mark executable {path}")
        return
    path.chmod(desired)


def vendor_botbrowser(src: Path, *, dry_run: bool) -> str:
    launcher = BOTBROWSER_DEST / "chromium-browser"
    if launcher.exists():
        _ensure_executable(launcher, dry_run=dry_run)
        print(
            "botbrowser: already present at "
            f"{BOTBROWSER_DEST} ({_format_size(_tree_size(BOTBROWSER_DEST))})"
        )
        return "skipped"

    if not src.is_dir():
        raise FileNotFoundError(
            f"BotBrowser source directory not found: {src}. "
            "Set LEGAL_BOTBROWSER_SRC or use --profiles-only."
        )

    print(f"botbrowser: source {src} ({_format_size(_tree_size(src))})")
    _ensure_dir(BOTBROWSER_DEST, dry_run=dry_run)
    copied, skipped = _copy_missing_tree(src, BOTBROWSER_DEST, dry_run=dry_run)
    _ensure_executable(launcher, dry_run=dry_run)

    if dry_run:
        print(
            "botbrowser: would copy missing files to "
            f"{BOTBROWSER_DEST} ({copied} copied, {skipped} skipped)"
        )
        return "dry-run"

    print(
        "botbrowser: vendored to "
        f"{BOTBROWSER_DEST} ({_format_size(_tree_size(BOTBROWSER_DEST))}; "
        f"{copied} copied, {skipped} skipped)"
    )
    return "vendored"


def vendor_profiles(src: Path, *, dry_run: bool) -> tuple[int, int]:
    if not src.is_dir():
        raise FileNotFoundError(
            f"BotBrowser profiles source directory not found: {src}. "
            "Set LEGAL_PROFILES_SRC."
        )

    profiles = sorted(src.glob("*.enc"))
    if not profiles:
        raise FileNotFoundError(f"No .enc profiles found in {src}")

    _ensure_dir(PROFILES_DEST, dry_run=dry_run)
    copied = 0
    skipped = 0
    for profile in profiles:
        target = PROFILES_DEST / profile.name
        if target.exists():
            skipped += 1
            continue

        copied += 1
        if not dry_run:
            shutil.copy2(profile, target)

    total_size = sum(profile.stat().st_size for profile in profiles)
    if dry_run:
        print(
            "profiles: would copy missing profiles to "
            f"{PROFILES_DEST} ({copied} copied, {skipped} skipped, "
            f"source {_format_size(total_size)})"
        )
    else:
        print(
            "profiles: vendored to "
            f"{PROFILES_DEST} ({copied} copied, {skipped} skipped, "
            f"{_format_size(_tree_size(PROFILES_DEST))})"
        )
    return copied, skipped


def seed_local_config(*, dry_run: bool) -> str:
    if LOCAL_CONFIG.exists():
        print(f"local_config: already present at {LOCAL_CONFIG}")
        return "skipped"

    if not LOCAL_CONFIG_EXAMPLE.is_file():
        raise FileNotFoundError(f"local config example not found: {LOCAL_CONFIG_EXAMPLE}")

    if dry_run:
        print(f"local_config: would copy {LOCAL_CONFIG_EXAMPLE} to {LOCAL_CONFIG}")
        return "dry-run"

    shutil.copy2(LOCAL_CONFIG_EXAMPLE, LOCAL_CONFIG)
    print(f"local_config: created {LOCAL_CONFIG}")
    print("local_config: set CAPSOLVER_API_KEY before running captcha-gated sources")
    return "created"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vendor BotBrowser assets and seed local config for legal."
    )
    parser.add_argument(
        "--profiles-only",
        action="store_true",
        help="skip the large BotBrowser binary copy and vendor only .enc profiles",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be copied without writing files",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    botbrowser_src = _path_from_env("LEGAL_BOTBROWSER_SRC", DEFAULT_BOTBROWSER_SRC)
    profiles_src = _path_from_env("LEGAL_PROFILES_SRC", DEFAULT_PROFILES_SRC)
    summary = BootstrapSummary()

    _ensure_dir(VENDOR_DIR, dry_run=args.dry_run)

    if args.profiles_only:
        summary.botbrowser = "skipped (--profiles-only)"
        print("botbrowser: skipped (--profiles-only)")
    else:
        summary.botbrowser = vendor_botbrowser(botbrowser_src, dry_run=args.dry_run)

    copied, skipped = vendor_profiles(profiles_src, dry_run=args.dry_run)
    summary.profiles_copied = copied
    summary.profiles_skipped = skipped
    summary.local_config = seed_local_config(dry_run=args.dry_run)

    print("")
    print("summary:")
    print(f"- vendor dir: {VENDOR_DIR}")
    print(f"- botbrowser: {summary.botbrowser}")
    print(
        "- profiles: "
        f"{summary.profiles_copied} copied, {summary.profiles_skipped} skipped"
    )
    print(f"- local_config.py: {summary.local_config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
