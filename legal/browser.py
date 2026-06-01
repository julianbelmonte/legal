"""BotBrowser launcher for browser-backed legal sources."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from playwright.sync_api import sync_playwright

from apps.legal import config


_BINDINGS_CLEANUP = (
    "delete window.__playwright__binding__; delete window.__pwInitScripts;"
)


def _start_xvfb() -> tuple[subprocess.Popen[bytes], str]:
    disp = 80 + int.from_bytes(os.urandom(1), "big") % 40
    proc = subprocess.Popen(
        ["Xvfb", f":{disp}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    return proc, str(disp)


class BotBrowser:
    """Minimal self-contained BotBrowser-via-Playwright launcher."""

    def __init__(self, hidden: bool = False):
        self.hidden = hidden
        self._xvfb: subprocess.Popen[bytes] | None = None
        self._pw: Any | None = None
        self.ctx: Any | None = None
        self.page: Any | None = None

    def __enter__(self) -> "BotBrowser":
        self._pw = sync_playwright().start()
        try:
            session_dir = Path(tempfile.mkdtemp(prefix="legal_bb_", dir="/tmp"))
            profile = config.pick_profile()
            enc = json.loads(profile.read_text())
            merged = {
                "configs": {
                    "timezone": "America/Argentina/Buenos_Aires",
                    "locale": "es-AR",
                    "languages": ["es-AR", "es"],
                },
                **enc,
            }
            merged_path = session_dir / "profile-merged.json"
            merged_path.write_text(json.dumps(merged))

            env = None
            if self.hidden:
                self._xvfb, disp = _start_xvfb()
                env = {**os.environ, "DISPLAY": f":{disp}"}

            self.ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(session_dir),
                executable_path=config.botbrowser_bin(),
                headless=False,
                env=env,
                ignore_default_args=[
                    "--disable-crash-reporter",
                    "--disable-crashpad-for-testing",
                    "--disable-gpu-watchdog",
                ],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--bot-profile={merged_path}",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-audio-output",
                ],
            )
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.add_init_script(_BINDINGS_CLEANUP)
            return self
        except Exception:
            self._stop()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop()

    def _stop(self) -> None:
        try:
            if self.ctx is not None:
                self.ctx.close()
        finally:
            self.ctx = None
            self.page = None
            try:
                if self._pw is not None:
                    self._pw.stop()
            finally:
                self._pw = None
                if self._xvfb is not None:
                    self._xvfb.terminate()
                    self._xvfb = None


__all__ = ["BotBrowser"]
