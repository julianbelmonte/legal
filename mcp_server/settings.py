"""Settings for the MCP server consumer.

Holds deploy-time, non-secret configuration selection for the remote MCP
server. Concrete environment parsing and OAuth fields land in later steps; this
module provides an importable settings container so the package skeleton is
loadable without network services or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class McpSettings:
    """Container for MCP server settings.

    Fields are filled in by later steps (public URL, OAuth issuer, allowed
    emails, signing/login secrets, client allowlist). The empty skeleton keeps
    the package importable for local development.
    """


def load_settings() -> McpSettings:
    """Return MCP settings.

    Stub: later steps read configuration from the environment with the
    ``LEGAL_MCP_`` prefix and merge the server-side ``LEGAL_API_KEY``.
    """
    return McpSettings()
