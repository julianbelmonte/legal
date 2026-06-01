"""Display-gated BotBrowser launch smoke for legal."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from legal import config
from legal.browser import BotBrowser


def _skip(reason: str) -> int:
    print(f"skip ({reason})")
    return 0


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def main() -> int:
    if os.environ.get("LEGAL_LIVE_SMOKE") != "1":
        return _skip("LEGAL_LIVE_SMOKE!=1")

    if shutil.which("Xvfb") is None:
        return _skip("Xvfb not found")

    try:
        browser_bin = config.botbrowser_bin()
        profile = config.pick_profile()
    except RuntimeError as exc:
        return _skip(str(exc))

    if not _is_executable(browser_bin):
        return _skip(f"BotBrowser executable not found: {browser_bin}")
    if not profile.is_file():
        return _skip(f"BotBrowser profile not found: {profile}")

    with BotBrowser(hidden=True) as bb:
        bb.page.goto("https://example.com", wait_until="domcontentloaded")
        title = bb.page.title()
        assert title

    print("launch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
