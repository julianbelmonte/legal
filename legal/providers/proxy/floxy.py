"""Floxy mobile proxy backend.

Standalone reimplementation of the webcam ``drone/network/floxy.py`` backend.
Credentials are resolved through the ``legal.config`` secret chain
(``floxy_user()`` / ``floxy_pass()``), never hardcoded. No ``drone`` import.
"""

from .base import ProxyBackend, generate_session_id


class FloxyBackend(ProxyBackend):
    name = "floxy"
    default_server = "mobile.floxy.io:8080"

    def build_proxy_url(self, country_code: str, session: str | None = None) -> str:
        from legal import config

        user = config.floxy_user()
        password = config.floxy_pass()
        session_id = session or generate_session_id()
        return (
            f"http://{user}-package-mobile-country-{country_code.upper()}"
            f"-session-{session_id}-time-20:{password}@mobile.floxy.io:8080"
        )

    def matches_url(self, proxy_url: str) -> bool:
        return "floxy.io" in proxy_url
