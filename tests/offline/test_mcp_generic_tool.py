"""Unit tests for the MCP generic run tool and its safety guard.

``legal_run_operation`` mirrors the API uniform route by delegating to
:func:`legal.dispatch.run_operation`, but first rejects MCP-inappropriate
parameters (filesystem save paths, raw PDF/binary requests, downloadable
artifacts) via :func:`reject_unsafe_mcp_params`. These tests prove the guard is
conservative-but-precise, that the tool delegates with the expected arguments
and serializes the envelope, and that rejected params yield a normalized
``usage_error`` envelope instead of raising.
"""

from __future__ import annotations

from inspect import signature

import pytest

import legal.dispatch
from mcp_server.tools import legal_run_operation
from mcp_server.tools import generic as generic_module
from mcp_server.tools.generic import (
    UnsafeMcpParamsError,
    reject_unsafe_mcp_params,
)


def test_legal_run_operation_signature_exposes_parameters() -> None:
    sig = signature(legal_run_operation)
    for name in ("source_id", "operation", "params", "raw"):
        assert name in sig.parameters
    assert sig.parameters["params"].default is None
    assert sig.parameters["raw"].default is False


def test_safe_params_are_not_rejected() -> None:
    # A normal search request must pass untouched.
    assert reject_unsafe_mcp_params("saij", "search", {"text": "x"}) is None
    assert reject_unsafe_mcp_params("csjn", "documento", {"id": "123"}) is None
    assert reject_unsafe_mcp_params("saij", "search", None) is None
    assert reject_unsafe_mcp_params("saij", "search", {}) is None
    # ``raw`` (raw provider payload) is JSON-serializable and allowed.
    assert reject_unsafe_mcp_params("saij", "search", {"text": "x"}) is None


@pytest.mark.parametrize(
    "key",
    [
        "save_pdf",
        "save-pdf",
        "savePdf",
        "save_path",
        "output_path",
        "out_path",
        "outfile",
        "download_path",
        "download",
        "pdf",
        "raw_pdf",
        "raw_bytes",
    ],
)
def test_unsafe_keys_are_rejected(key: str) -> None:
    with pytest.raises(UnsafeMcpParamsError) as excinfo:
        reject_unsafe_mcp_params("csjn", "download", {key: "/tmp/a.pdf"})
    assert excinfo.value.key == key
    assert excinfo.value.source_id == "csjn"
    assert excinfo.value.operation == "download"


def test_legal_run_operation_delegates_to_dispatch(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeResponse:
        def to_dict(self) -> dict[str, object]:
            return {"ok": True, "source": "saij", "operation": "search"}

    def fake_run_operation(source_id, operation, params, *, raw=False):
        calls["source_id"] = source_id
        calls["operation"] = operation
        calls["params"] = params
        calls["raw"] = raw
        return FakeResponse()

    monkeypatch.setattr(
        generic_module.legal.dispatch, "run_operation", fake_run_operation
    )

    result = legal_run_operation("saij", "search", {"text": "despido"}, raw=True)

    assert calls == {
        "source_id": "saij",
        "operation": "search",
        "params": {"text": "despido"},
        "raw": True,
    }
    assert result == {"ok": True, "source": "saij", "operation": "search"}


def test_legal_run_operation_rejects_unsafe_params_as_envelope(monkeypatch) -> None:
    def fail(*_args, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("dispatch must not run for unsafe params")

    monkeypatch.setattr(generic_module.legal.dispatch, "run_operation", fail)

    result = legal_run_operation("csjn", "download", {"save_pdf": "/tmp/a.pdf"})

    assert result["ok"] is False
    assert result["source"] == "csjn"
    assert result["operation"] == "download"
    assert result["error"]["code"] == "usage_error"


def test_module_attribute_is_patchable() -> None:
    # The tool references dispatch as a module attribute so tests/consumers can
    # patch it the same way the API route does.
    assert (
        generic_module.legal.dispatch.run_operation
        is legal.dispatch.run_operation
    )
