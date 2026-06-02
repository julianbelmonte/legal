"""JSON envelope serialization helpers for the MCP server.

The MCP server returns JSON-compatible normalized envelopes that preserve the
pipeline's envelope keys (``ok``, ``source``, ``operation``, ``query``,
``document``, ``page``, ``provenance``, ``warnings``, ``error``). Concrete
helpers land in a later step; this module is an importable stub so the package
skeleton loads.
"""

from __future__ import annotations
