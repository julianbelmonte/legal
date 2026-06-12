"""Domain (Caddy) public-URL / env / DNS helpers for the legal VPS deploy.

The production deploy fronts the combined ASGI app with Caddy on a stable
domain (default ``mcp.arglegal.live``). Caddy presents the MCP transport at the
domain ROOT (it rewrites ``/`` -> ``/mcp/``), so the connector / OAuth-resource
URL is the **bare domain** with no ``/mcp`` suffix: ``server/settings.py``
``resource()`` returns the public URL verbatim and ``issuer()`` defaults to it,
so the advertised OAuth metadata must equal the connector URL.

This is standalone deploy tooling: it does not import the legal pipeline's
source-access internals.
"""

from __future__ import annotations

#: Env var the MCP server reads for its public URL (the connector URL).
MCP_PUBLIC_URL_ENV_VAR = "LEGAL_MCP_PUBLIC_URL"

#: Env var the MCP server reads for the OAuth issuer.
MCP_OAUTH_ISSUER_ENV_VAR = "LEGAL_MCP_OAUTH_ISSUER"


def public_url_for_domain(domain: str) -> str:
    """Return the bare-domain public URL ``https://<domain>`` (no ``/mcp``).

    Caddy serves the MCP transport at the domain root, so the connector URL and
    the OAuth protected-resource URL are just ``https://<domain>``.
    """
    host = domain.strip().strip("/")
    if not host:
        raise ValueError("domain must be a non-empty hostname")
    return f"https://{host}"


def oauth_env_updates_for_domain(domain: str) -> dict[str, str]:
    """Return the env updates pointing OAuth metadata at the bare domain.

    Both ``LEGAL_MCP_PUBLIC_URL`` and ``LEGAL_MCP_OAUTH_ISSUER`` are the bare
    ``https://<domain>`` so issuer == resource == public URL == the connector
    URL (``server/settings.py`` ``resource()`` returns ``str(public_url)`` and
    ``issuer()`` defaults to it). Appending ``/mcp`` here would make the
    advertised OAuth resource mismatch the connector URL — do not.
    """
    url = public_url_for_domain(domain)
    return {
        MCP_PUBLIC_URL_ENV_VAR: url,
        MCP_OAUTH_ISSUER_ENV_VAR: url,
    }


def dns_host_label(domain: str) -> str:
    """Return the Namecheap ``--host`` label for ``domain``.

    A subdomain like ``mcp.arglegal.live`` -> ``"mcp"``; an apex like
    ``arglegal.live`` -> ``"@"``. Used to build the ``nc_browser.py dns-set-ip``
    invocation that repoints the A record at a (new) VPS IP. Assumes a
    single-label public suffix (true for ``.live``); multi-part suffixes such as
    ``co.uk`` are out of scope for this deploy.
    """
    host = domain.strip().strip(".")
    if not host:
        raise ValueError("domain must be a non-empty hostname")
    labels = host.split(".")
    if len(labels) <= 2:
        return "@"
    return labels[0]


def registered_domain(domain: str) -> str:
    """Return the registered (apex) domain for ``domain``.

    ``mcp.arglegal.live`` -> ``"arglegal.live"``; an apex ``arglegal.live`` is
    returned unchanged. The Namecheap DNS UI is managed at the registered-domain
    level, so the ``nc_browser.py dns-set-ip`` call takes this apex plus the
    ``--host`` label from :func:`dns_host_label` (not the full subdomain).
    Assumes a single-label public suffix (true for ``.live``).
    """
    host = domain.strip().strip(".")
    if not host:
        raise ValueError("domain must be a non-empty hostname")
    return ".".join(host.split(".")[-2:])
