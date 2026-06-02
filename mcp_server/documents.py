"""Document text handling for the MCP server.

The MCP server never exposes PDF downloads. It may fetch PDFs internally to
extract document text, cache the text with a TTL, and return explicit text
pages with opaque cursors instead of silently truncating. The resolver map,
cache records, cursor logic, initial retrieval, continuation, and in-document
search land in later steps; this module is an importable stub so the package
skeleton loads.
"""

from __future__ import annotations
