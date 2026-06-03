"""Unit tests for the MCP discovery tools.

These tools mirror the API discovery routes and reuse :mod:`legal.registry` and
:mod:`legal.schema`. The tests exercise both the real registry/schema (to prove
the 1:1 mirror) and mocked registry calls (to prove the tools delegate to the
seams rather than embedding source-access logic).
"""

from __future__ import annotations

import legal.registry
import legal.schema
from server.tools import legal_schema, legal_source, legal_sources
from server.tools import discovery


def test_legal_sources_mirrors_registry() -> None:
    result = legal_sources()
    assert isinstance(result, dict)
    assert result == {"items": legal.registry.list_sources()}
    assert result["items"], "expected at least one wired source"
    first = result["items"][0]
    # Provenance/discovery metadata is present on each source.
    assert "id" in first
    assert "operations" in first


def test_legal_sources_delegates_to_registry(monkeypatch) -> None:
    sentinel = [{"id": "demo", "name": "Demo", "operations": ["search"]}]
    monkeypatch.setattr(legal.registry, "list_sources", lambda: sentinel)
    result = legal_sources()
    assert result == {"items": sentinel}


def test_legal_source_returns_known_source() -> None:
    source_id = legal.registry.SOURCE_IDS[0]
    result = legal_source(source_id)
    assert result == legal.registry.get_source(source_id).to_dict()
    assert result["id"] == source_id


def test_legal_source_unknown_returns_not_found_envelope() -> None:
    result = legal_source("does-not-exist")
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert result["source"] == "does-not-exist"
    assert result["operation"] == "get_source"


def test_legal_source_delegates_to_registry(monkeypatch) -> None:
    calls = {}

    def fake_get_source(source_id: str):
        calls["source_id"] = source_id
        return None

    monkeypatch.setattr(discovery.legal.registry, "get_source", fake_get_source)
    result = legal_source("demo")
    assert calls["source_id"] == "demo"
    assert result["ok"] is False


def test_legal_schema_mirrors_schema() -> None:
    result = legal_schema()
    assert isinstance(result, dict)
    assert result == legal.schema.LEGAL_RESPONSE_SCHEMA
    assert result["title"] == "Legal CLI agent response"
