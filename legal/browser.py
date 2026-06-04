"""BotBrowser launcher for browser-backed legal sources."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from legal import config
from legal.errors import source_unavailable


_BINDINGS_CLEANUP = (
    "delete window.__playwright__binding__; delete window.__pwInitScripts;"
)

# Fail-fast page defaults: a flaky proxy exit stalls navigation, and waiting the
# Playwright default (30s) per step multiplies into agent-side timeouts. Bound
# navigation/actions tighter so a dead exit is abandoned quickly and a fresh one
# can be tried by ``run_with_botbrowser``.
DEFAULT_NAV_TIMEOUT_MS = 15000
DEFAULT_ACTION_TIMEOUT_MS = 15000

# Browser-launch / page errors that mean "this exit or attempt failed; rotating
# to a fresh proxy exit may recover".
RETRYABLE_BROWSER_EXCEPTIONS = (PlaywrightTimeoutError, PlaywrightError)


class BrowserRetry(Exception):
    """Raised by an attempt to ask ``run_with_botbrowser`` for a fresh exit.

    Carries optional ``meta`` describing the soft failure (recaptcha rejected,
    navigation did not complete, WAF status, ...), surfaced to the caller when
    every attempt is exhausted.
    """

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        super().__init__("browser attempt did not succeed; rotate exit")
        self.meta: dict[str, Any] = dict(meta or {})


class BrowserExhausted(Exception):
    """Raised by ``run_with_botbrowser`` when no attempt/exit succeeded.

    ``meta`` holds the last attempt's failure detail (including ``exits_tried``)
    so the caller can build a transparent, retryable error envelope.
    """

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        super().__init__("browser search did not complete after rotating exits")
        self.meta: dict[str, Any] = dict(meta or {})


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

            from legal.providers import proxy as _proxy

            proxy_pw = _proxy.resolve_proxy_for_playwright()
            if proxy_pw is not None:
                merged.setdefault("configs", {})["proxy"] = dict(proxy_pw)

            merged_path = session_dir / "profile-merged.json"
            merged_path.write_text(json.dumps(merged))

            env = None
            if self.hidden:
                self._xvfb, disp = _start_xvfb()
                env = {**os.environ, "DISPLAY": f":{disp}"}

            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(session_dir),
                "executable_path": config.botbrowser_bin(),
                "headless": False,
                "env": env,
                "ignore_default_args": [
                    "--disable-crash-reporter",
                    "--disable-crashpad-for-testing",
                    "--disable-gpu-watchdog",
                ],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    f"--bot-profile={merged_path}",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-audio-output",
                ],
            }
            if proxy_pw is not None:
                launch_kwargs["proxy"] = proxy_pw

            self.ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.add_init_script(_BINDINGS_CLEANUP)
            # Fail fast on a stalled exit instead of hanging the Playwright
            # default; callers can still pass an explicit per-call timeout.
            self.page.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
            self.page.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
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


def run_with_botbrowser(
    attempt: "Callable[[Any, int], Any]",
    *,
    retries: int = 3,
    hidden: bool = True,
    launcher: "Callable[[bool], Any] | None" = None,
) -> Any:
    """Run ``attempt(page, attempt_index)`` with a fresh BotBrowser per try.

    Each try launches a new BotBrowser context, which resolves a **fresh proxy
    exit** (``resolve_proxy_for_playwright`` mints a new sticky session per
    launch). So when the proxy is active, a failed exit is abandoned and the
    next try runs behind a different exit — the recovery a same-context retry
    loop can never achieve. When the proxy is disabled this still retries, just
    without a new exit (a no-op rotation).

    ``attempt`` returns its result on success, or raises :class:`BrowserRetry`
    (optionally carrying failure ``meta``) to request another exit. A launch or
    page timeout/error is treated the same way. When all tries are exhausted,
    :class:`BrowserExhausted` is raised carrying the last failure ``meta`` plus
    ``exits_tried`` for a transparent, retryable envelope.

    ``launcher`` overrides how a browser context is created (tests inject a fake
    that needs no real Chromium); it defaults to :class:`BotBrowser`.
    """
    make = launcher or (lambda h: BotBrowser(hidden=h))
    tries = max(1, retries)
    last_meta: dict[str, Any] = {}
    for index in range(tries):
        try:
            with make(hidden) as bb:
                return attempt(bb.page, index + 1)
        except BrowserRetry as retry:
            last_meta = {**retry.meta, "attempt": index + 1, "exits_tried": index + 1}
        except RETRYABLE_BROWSER_EXCEPTIONS as exc:
            last_meta = {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "attempt": index + 1,
                "exits_tried": index + 1,
            }
    raise BrowserExhausted(last_meta)


def browser_unavailable_error(meta: dict[str, Any], *, source: str, operation: str) -> Any:
    """Build a transparent, retryable error for an exhausted browser search.

    Names the proxy-exit rotation so the agent learns the failure was an
    upstream connectivity/exit problem (retry shortly), not an empty result.
    """
    exits = meta.get("exits_tried")
    detail = meta.get("error") or "no accepted result"
    suffix = f" after {exits} proxy exit(s)" if exits else ""
    return source_unavailable(
        f"{source} {operation} did not complete{suffix}: {detail}",
        details={"meta": meta},
    )


__all__ = [
    "BotBrowser",
    "BrowserExhausted",
    "BrowserRetry",
    "browser_unavailable_error",
    "run_with_botbrowser",
]
