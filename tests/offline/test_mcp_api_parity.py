"""Parity tests: MCP tools mirror the API/dispatch normalized envelope.

The MCP generic tool (``legal_run_operation``) and the MCP global search tool
(``legal_search``) reuse the *same* internal functions the API/dispatch layer
uses:

* ``legal.dispatch.run_operation`` -- the uniform dispatch seam behind both the
  MCP generic tool and the API uniform route
  (``POST /v1/sources/{source_id}/{operation}``).
* ``legal.global_search.run_global_search`` -- the shared global-search core
  behind both the MCP search tool and the API ``POST /v1/search`` route.

Both consumers reference those callables as module attributes, so a single
``monkeypatch`` of the underlying function intercepts every consumer at once.
These tests patch the seam to return known fixture envelopes -- both **success**
and **failure/error** shapes -- and then assert two things:

1. The MCP tool returns exactly ``to_jsonable(<patched return value>)`` -- the
   normalized JSON envelope, unchanged.
2. That MCP output equals, byte-for-byte, the JSON body the API route returns
   for the same patched seam (proving true envelope parity, not just that each
   side independently serializes the same fixture).

Tests are named so ``-k "parity or generic or global_search"`` selects them.
"""

from __future__ import annotations

from typing import Any

import pytest

import legal.dispatch
import legal.global_search
from mcp_server.serialization import to_jsonable
from mcp_server.tools import legal_run_operation, legal_search


# --------------------------------------------------------------------------- #
# Fixture envelopes (success + failure) returned by the patched seam.
# A plain mapping is accepted everywhere a ``LegalResponse`` would be, and a
# fake ``to_dict``-bearing object proves the serialization path is exercised the
# same way on both sides.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """Minimal stand-in for ``LegalResponse``: exposes ``to_dict`` only."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


def _ok_generic_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "source": "saij",
        "operation": "search",
        "query": {"text": "despido"},
        "items": [{"id": "abc", "title": "Fallo"}],
        "page": {"limit": 5, "offset": 0, "total": 1},
        "provenance": {"source_id": "saij"},
        "warnings": [],
    }


def _error_generic_envelope() -> dict[str, Any]:
    return {
        "ok": False,
        "source": "saij",
        "operation": "search",
        "error": {
            "code": "usage_error",
            "message": "missing required parameter",
            "retryable": False,
        },
    }


def _ok_global_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "source": "legal",
        "operation": "search",
        "query": {"text": "despido"},
        "items": [{"id": "x", "_source": "saij"}],
        "facets": {"saij": {}},
        "page": {"limit_per_source": 5},
        "provenance": {"sources": ["saij"]},
        "warnings": [],
    }


def _error_global_envelope() -> dict[str, Any]:
    return {
        "ok": False,
        "source": "legal",
        "operation": "search",
        "error": {
            "code": "source_unavailable",
            "message": "all selected source searches failed",
            "retryable": False,
        },
    }


# --------------------------------------------------------------------------- #
# 1. Generic tool parity: MCP legal_run_operation == serialized dispatch return.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "envelope",
    [_ok_generic_envelope(), _error_generic_envelope()],
    ids=["success", "failure"],
)
def test_generic_tool_matches_serialized_dispatch_return(
    envelope: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP generic tool output == ``to_jsonable`` of the dispatch return value."""
    fake = _FakeResponse(envelope)
    monkeypatch.setattr(legal.dispatch, "run_operation", lambda *a, **k: fake)

    result = legal_run_operation("saij", "search", {"text": "despido"})

    assert result == to_jsonable(fake)
    assert result == envelope


@pytest.mark.parametrize(
    "envelope",
    [_ok_generic_envelope(), _error_generic_envelope()],
    ids=["success", "failure"],
)
def test_generic_tool_matches_api_route_envelope(
    envelope: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """MCP generic tool output == the API uniform route's JSON body.

    Both the MCP tool and ``api.runner.run`` (behind the generic route) call
    ``legal.dispatch.run_operation`` as a module attribute, so one patch drives
    both. The API may pick a non-200 HTTP status for failure envelopes, but the
    JSON *body* is the same normalized envelope the MCP tool returns. The seam
    returns a plain mapping (accepted unchanged by both the MCP serializer and
    the API's ``envelope_and_status``) so the comparison is byte-for-byte.
    """
    client, headers = api_client
    monkeypatch.setattr(legal.dispatch, "run_operation", lambda *a, **k: dict(envelope))

    mcp_result = legal_run_operation("saij", "search", {"text": "despido"})

    resp = client.post(
        "/v1/sources/saij/search",
        json={"params": {"text": "despido"}},
        headers=headers,
    )
    assert resp.json() == mcp_result


# --------------------------------------------------------------------------- #
# 2. Global search parity: MCP legal_search == serialized run_global_search.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "envelope",
    [_ok_global_envelope(), _error_global_envelope()],
    ids=["success", "failure"],
)
def test_global_search_tool_matches_serialized_core_return(
    envelope: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool output == ``to_jsonable`` of the global-search return.

    Parity is byte-for-byte in ``raw=True`` mode; ``raw=False`` (the default)
    deliberately trims items to a token-lean shape, tested separately.
    """
    fake = _FakeResponse(envelope)
    monkeypatch.setattr(legal.global_search, "run_global_search", lambda **k: fake)

    result = legal_search(text="despido", all_direct=True, raw=True)

    assert result == to_jsonable(fake)
    assert result == envelope


@pytest.mark.parametrize(
    "envelope",
    [_ok_global_envelope(), _error_global_envelope()],
    ids=["success", "failure"],
)
def test_global_search_tool_matches_api_route_envelope(
    envelope: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    api_client: tuple[Any, dict[str, str]],
) -> None:
    """MCP search tool output == the API ``/v1/search`` route's JSON body.

    Both the MCP tool and the ``/v1/search`` router call
    ``legal.global_search.run_global_search`` as a module attribute, so one
    patch drives both; the JSON bodies must match exactly. The seam returns a
    plain mapping (accepted unchanged by both the MCP serializer and the API's
    ``envelope_and_status``) so the comparison is byte-for-byte.
    """
    client, headers = api_client
    monkeypatch.setattr(
        legal.global_search, "run_global_search", lambda **k: dict(envelope)
    )

    mcp_result = legal_search(text="despido", all_direct=True, raw=True)

    resp = client.post(
        "/v1/search",
        json={"text": "despido", "all_direct": True, "raw": True},
        headers=headers,
    )
    assert resp.json() == mcp_result


def test_global_search_tool_trims_items_when_not_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw=False (default) reduces items to the lean shape; provenance kept once."""
    rich = {
        "ok": True,
        "source": "legal",
        "operation": "search",
        "items": [
            {
                "id": "saij:x",
                "title": "T",
                "date": "2024-01-01",
                "document_type": "Sumario",
                "url": "https://example/x",
                "snippet": "s",
                "source_fields": {"content": {"big": "x" * 2000}},
                "facets": {"descriptores": ["a"] * 50},
                "provenance": {"raw": {"documentScore": 1.5, "explain": "..."}},
            }
        ],
        "provenance": {"source_urls": []},
    }
    monkeypatch.setattr(
        legal.global_search, "run_global_search", lambda **k: dict(rich)
    )

    lean = legal_search(text="x", all_direct=True)  # raw defaults to False

    item = lean["items"][0]
    assert set(item) == {"id", "title", "date", "type", "url", "snippet", "score"}
    assert item["score"] == 1.5
    assert item["type"] == "Sumario"
    assert "source_fields" not in item and "provenance" not in item
    assert "provenance" in lean  # response-level provenance kept once
