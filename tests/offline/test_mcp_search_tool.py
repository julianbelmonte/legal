"""Unit tests for the MCP global search tool.

The ``legal_search`` tool reuses the shared global-search core
(:func:`legal.global_search.run_global_search`) -- the same function behind the
API ``POST /v1/search`` route and the CLI ``search`` command. These tests prove
the tool delegates to that core with the expected parameters, serializes the
normalized envelope, and passes through both the success and total-failure
shapes the core produces.
"""

from __future__ import annotations

from inspect import signature

import legal.global_search
from mcp_server.tools import legal_search
from mcp_server.tools import search as search_module


def test_legal_search_signature_exposes_parameters() -> None:
    sig = signature(legal_search)
    for name in ("text", "sources", "all_direct", "limit_per_source", "raw"):
        assert name in sig.parameters
    # Conservative defaults: no source preselected, narrow per-source limit.
    assert sig.parameters["sources"].default is None
    assert sig.parameters["all_direct"].default is False
    assert sig.parameters["limit_per_source"].default == 5
    assert sig.parameters["raw"].default is False


def test_legal_search_delegates_to_core(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeResponse:
        def to_dict(self) -> dict[str, object]:
            return {
                "ok": True,
                "source": "legal",
                "operation": "search",
                "items": [{"id": "x", "_source": "saij"}],
            }

    def fake_run_global_search(**kwargs):
        calls.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(
        search_module.legal.global_search,
        "run_global_search",
        fake_run_global_search,
    )

    result = legal_search(
        text="despido",
        sources=["saij"],
        all_direct=False,
        limit_per_source=3,
        raw=True,
    )

    assert calls == {
        "text": "despido",
        "sources": ["saij"],
        "all_direct": False,
        "limit_per_source": 3,
        "raw": True,
    }
    assert result == {
        "ok": True,
        "source": "legal",
        "operation": "search",
        "items": [{"id": "x", "_source": "saij"}],
    }


def test_legal_search_passes_through_total_failure(monkeypatch) -> None:
    error_envelope = {
        "ok": False,
        "source": "legal",
        "operation": "search",
        "error": {
            "code": "source_unavailable",
            "message": "all selected source searches failed",
            "retryable": False,
        },
    }

    class FakeResponse:
        def to_dict(self) -> dict[str, object]:
            return error_envelope

    monkeypatch.setattr(
        search_module.legal.global_search,
        "run_global_search",
        lambda **_: FakeResponse(),
    )

    result = legal_search(text="despido", all_direct=True)

    assert result["ok"] is False
    assert result["error"]["code"] == "source_unavailable"
    assert result == error_envelope


def test_legal_search_module_attribute_is_patchable() -> None:
    # The tool references the core as a module attribute so tests/consumers can
    # patch it the same way the API route does.
    assert hasattr(search_module.legal.global_search, "run_global_search")
    assert (
        search_module.legal.global_search.run_global_search
        is legal.global_search.run_global_search
    )
