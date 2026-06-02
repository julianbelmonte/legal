"""OAuth authentication for the MCP server.

The remote MCP endpoint is public HTTPS but protected by OAuth bearer tokens
with a single-user allowlist. OAuth settings, token models, the single-user
provider, discovery/metadata endpoints, and bearer validation land in later
steps; this module is an importable stub so the package skeleton loads.
"""

from __future__ import annotations
