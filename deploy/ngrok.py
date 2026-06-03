"""ngrok tunnel configuration and public-URL discovery for the legal VPS.

Before buying a domain + certificate, Claude Cowork testing needs a public,
trusted HTTPS URL pointing at the combined ASGI app (``api.main:app``) running
on the VPS. ngrok provides that: the bootstrap (:mod:`deploy.bootstrap`)
installs ngrok and runs ``ngrok http <port>`` as a systemd unit, and this
module renders the start command / authtoken-config command and discovers the
resulting public HTTPS URL from ngrok's local agent API.

Two roles:

- **Render-time** (no network): :func:`render_authtoken_command` and
  :func:`render_start_command` produce the shell the orchestrator runs on the
  VPS. The authtoken is configured out-of-band via ``ngrok config
  add-authtoken`` and is **never** printed or embedded in logged output -- see
  :func:`render_start_command`, which never receives the token at all.
- **Runtime on the VPS** (network): :func:`discover_public_url` queries ngrok's
  local agent API (``GET /api/tunnels``) for the running tunnel's https
  ``public_url``. :func:`mcp_url_from_tunnel_url` turns that base into the MCP
  endpoint, and :func:`oauth_env_updates` turns it into the env updates the
  orchestrator applies so the runtime OAuth metadata advertises the discovered
  public URL.

This is standalone deploy tooling: it does not import the legal pipeline's
source-access internals.
"""

from __future__ import annotations

import shlex
from typing import Any

import httpx

#: Default address of ngrok's local agent API (the web inspect interface).
DEFAULT_AGENT_API = "http://127.0.0.1:4040"

#: Default timeout (seconds) for agent-API queries.
DEFAULT_TIMEOUT = 10.0

#: Env var the MCP server reads for its public URL (the ``/mcp`` endpoint).
MCP_PUBLIC_URL_ENV_VAR = "LEGAL_MCP_PUBLIC_URL"

#: Env var the MCP server reads for the OAuth issuer (the tunnel base URL).
MCP_OAUTH_ISSUER_ENV_VAR = "LEGAL_MCP_OAUTH_ISSUER"

#: MCP endpoint path appended to the tunnel base.
MCP_PATH = "/mcp"


class NgrokError(RuntimeError):
    """Raised when the ngrok agent API is unreachable or has no tunnel."""


def mcp_url_from_tunnel_url(tunnel_url: str) -> str:
    """Return the MCP endpoint URL for a discovered ngrok ``tunnel_url``.

    Appends ``/mcp`` to the tunnel base, normalizing trailing slashes so the
    result never contains a double slash, and is idempotent when the URL
    already ends in ``/mcp``::

        mcp_url_from_tunnel_url("https://x.ngrok.app")  == "https://x.ngrok.app/mcp"
        mcp_url_from_tunnel_url("https://x.ngrok.app/") == "https://x.ngrok.app/mcp"
        mcp_url_from_tunnel_url("https://x.ngrok.app/mcp/") == "https://x.ngrok.app/mcp"

    :param tunnel_url: The ngrok public base URL (or an already-``/mcp`` URL).
    :raises ValueError: If ``tunnel_url`` is empty.
    """
    base = tunnel_url.strip().rstrip("/")
    if not base:
        raise ValueError("tunnel_url must be a non-empty URL")
    if base.endswith(MCP_PATH):
        return base
    return f"{base}{MCP_PATH}"


def tunnel_base_from_url(url: str) -> str:
    """Return the tunnel base (no ``/mcp`` suffix, no trailing slash).

    Inverse of :func:`mcp_url_from_tunnel_url`: strips a trailing ``/mcp`` and
    any trailing slash so the result is usable as the OAuth issuer base.
    """
    base = url.strip().rstrip("/")
    if base.endswith(MCP_PATH):
        base = base[: -len(MCP_PATH)].rstrip("/")
    return base


def render_authtoken_command(authtoken: str) -> str:
    """Render the ``ngrok config add-authtoken`` command for ``authtoken``.

    The returned string contains the (shell-quoted) authtoken because the
    command must configure it; callers MUST NOT log or print this string. Use
    :func:`render_start_command` (which never sees the token) for anything that
    appears in output.
    """
    if not authtoken:
        raise ValueError("authtoken must be a non-empty string")
    return f"ngrok config add-authtoken {shlex.quote(authtoken)}"


def render_start_command(port: int, *, log: str = "stdout") -> str:
    """Render the ``ngrok http <port>`` start command.

    The authtoken is intentionally **not** a parameter here: it is configured
    out-of-band via :func:`render_authtoken_command`, so this command is safe to
    print or log. The tunnel inherits the authtoken from ngrok's config.

    :param port: Local service port to tunnel.
    :param log: ngrok ``--log`` target (``stdout`` so systemd captures it).
    """
    port = int(port)
    if port <= 0:
        raise ValueError("port must be a positive integer")
    cmd = f"ngrok http {port}"
    if log:
        cmd += f" --log={shlex.quote(log)}"
    return cmd


def _select_https_url(tunnels: Any) -> str | None:
    """Return the preferred public URL from a tunnels payload list.

    Prefers an ``https://`` tunnel; falls back to the first ``public_url`` seen.
    """
    if not isinstance(tunnels, list):
        return None
    fallback: str | None = None
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        url = tunnel.get("public_url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith("https://"):
            return url
        fallback = fallback or url
    return fallback


def public_url_from_payload(payload: Any) -> str | None:
    """Return the preferred https public URL from an ngrok tunnels payload.

    Accepts the parsed JSON body of ``GET /api/tunnels`` (a dict with a
    ``tunnels`` list). Returns ``None`` if no tunnel/public URL is present.
    """
    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    return _select_https_url(tunnels)


def discover_public_url(
    api_addr: str = DEFAULT_AGENT_API,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> str:
    """Discover the running tunnel's public https URL from ngrok's local API.

    Queries ``GET <api_addr>/api/tunnels`` and returns the https ``public_url``
    of the running tunnel (preferring the https tunnel). This is for runtime use
    on the VPS, where ngrok's agent API is reachable on localhost; it is not
    expected to succeed offline.

    :param api_addr: Base address of ngrok's local agent API.
    :param timeout: Request timeout in seconds.
    :param client: Optional pre-built ``httpx.Client`` (mainly for testing); if
        omitted, a short-lived client is created and closed.
    :raises NgrokError: If the agent API is unreachable, returns a non-2xx
        status, an unparseable body, or has no tunnel with a public URL.
    """
    url = f"{api_addr.rstrip('/')}/api/tunnels"
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NgrokError(f"ngrok agent API unreachable at {url}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise NgrokError(f"ngrok agent API returned a non-JSON body: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    public = public_url_from_payload(payload)
    if not public:
        raise NgrokError("no running ngrok tunnel with a public URL was found")
    return public


def oauth_env_updates(tunnel_url: str) -> dict[str, str]:
    """Return the env updates that point runtime OAuth metadata at the tunnel.

    Given a discovered tunnel URL, returns the ``KEY -> VALUE`` mapping the
    orchestrator merges into the app's env file so the MCP server advertises the
    public URL: ``LEGAL_MCP_PUBLIC_URL`` is the ``/mcp`` endpoint and
    ``LEGAL_MCP_OAUTH_ISSUER`` is the tunnel base.
    """
    return {
        MCP_PUBLIC_URL_ENV_VAR: mcp_url_from_tunnel_url(tunnel_url),
        MCP_OAUTH_ISSUER_ENV_VAR: tunnel_base_from_url(tunnel_url),
    }
