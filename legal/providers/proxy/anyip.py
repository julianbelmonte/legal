"""AnyIP residential/mobile proxy backend.

Standalone reimplementation of the webcam ``drone/network/anyip.py`` backend.
Credentials are resolved through the ``legal.config`` secret chain
(``anyip_user()`` / ``anyip_pass()``), never hardcoded. No ``drone`` import.

AnyIP is used as an HTTP proxy (Chromium cannot do SOCKS5 with auth), with the
country/session flags carried in the username, comma-separated:
``{USER},type_mobile,country_AR,session_{alnum}``.
"""

import re

from .base import ProxyBackend, generate_session_id

# AnyIP requires the session name to be strictly alphanumeric; punctuation
# (dashes/underscores) silently breaks the session bind so every request gets a
# different IP. Strip everything but [A-Za-z0-9].
_NONALNUM = re.compile(r"[^A-Za-z0-9]")


class AnyIPBackend(ProxyBackend):
    name = "anyip"
    default_server = "portal.anyip.io:1080"

    def build_proxy_url(self, country_code: str, session: str | None = None) -> str:
        from legal import config

        user = config.anyip_user()
        password = config.anyip_pass()
        session_id = _NONALNUM.sub("", session or generate_session_id())
        flags = ",".join(
            [
                "type_mobile",
                f"country_{country_code.upper()}",
                f"session_{session_id}",
            ]
        )
        return f"http://{user},{flags}:{password}@portal.anyip.io:1080"

    def matches_url(self, proxy_url: str) -> bool:
        return "anyip.io" in proxy_url
