"""MCP tool definitions for the legal pipeline.

The compact MCP tool surface (``legal_sources``, ``legal_source``,
``legal_schema``, ``legal_search``, ``legal_run_operation``,
``legal_get_document_text``, ``legal_get_document_text_page``,
``legal_find_in_document_text``) reuses the existing pipeline seams and adds no
source-access logic. Tool registration and behavior land in later steps; this
module is an importable stub so the package skeleton loads.
"""

from __future__ import annotations
