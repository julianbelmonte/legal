"""Null proxy backend — explicit "no proxy" egress.

Lets deploy settings express "proxy disabled/none" through the same
:class:`ProxyBackend` interface. Callers must check ``proxy_enabled`` /
``provider != "none"`` before asking for a URL; doing so here raises.
"""

from .base import ProxyBackend


class NullProxyBackend(ProxyBackend):
    name = "none"

    def build_proxy_url(self, country_code: str, session: str | None = None) -> str:
        raise RuntimeError(
            "NullProxyBackend has no proxy URL; check proxy_enabled / provider "
            "before requesting one."
        )

    def matches_url(self, proxy_url: str) -> bool:
        return False
