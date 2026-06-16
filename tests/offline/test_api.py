"""Offline FastAPI surface tests: auth, error mapping, routing parity.

These tests exercise the API end-to-end through a ``TestClient`` with the
pipeline seams mocked so no network or credentials are touched:

* ``legal.dispatch.run_operation`` — the uniform dispatch seam every source
  operation (generic + typed routers) flows through. ``api.runner.run``
  references it as a module attribute, so patching ``legal.dispatch.run_operation``
  intercepts every wired source/operation.
* ``legal.global_search.run_global_search`` — the shared global-search core the
  ``/v1/search`` router calls (also referenced as a module attribute).

The body is always the normalized envelope; we only assert the HTTP status the
API derives from the envelope's error code and that the right
``(source, operation)`` / params reach the mocked seam.
"""

from __future__ import annotations

from typing import Any

import pytest

import legal.dispatch
import legal.global_search
import legal.registry
import legal.schema
from legal.errors import usage_error

# Concrete path-param substitutions so we can exercise parameterized /v1 routes
# in the auth sweep without depending on any particular source being special.
_PATH_PARAM_VALUES = {"source_id": "saij", "operation": "search"}


def _ok_envelope(source: str = "saij", operation: str = "search") -> dict[str, Any]:
    """A minimal ``ok: true`` normalized envelope (a plain dict is accepted by
    ``envelope_and_status`` via its ``Mapping`` branch)."""
    return {
        "ok": True,
        "source": source,
        "operation": operation,
        "items": [],
    }


def _error_envelope(code: str, source: str = "saij", operation: str = "search") -> dict[str, Any]:
    """A normalized ``ok: false`` envelope carrying ``error.code``."""
    return {
        "ok": False,
        "source": source,
        "operation": operation,
        "error": {"code": code, "message": f"{code} for test", "retryable": False},
    }


def _v1_routes(app: Any) -> list[tuple[str, str]]:
    """Return ``(method, concrete_path)`` for every ``/v1`` route on the app."""
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not path.startswith("/v1") or not methods:
            continue
        concrete = path
        for name, value in _PATH_PARAM_VALUES.items():
            concrete = concrete.replace("{" + name + "}", value)
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.append((method, concrete))
    return routes


def _first_supported_op(source: dict[str, Any]) -> str | None:
    """First advertised operation that is not in ``unsupported_operations``."""
    unsupported = set(source.get("unsupported_operations") or [])
    for op in source.get("operations", []):
        if op not in unsupported:
            return op
    return None


# --------------------------------------------------------------------------- #
# 1. Auth: fail-closed on every /v1 route, open /healthz
# --------------------------------------------------------------------------- #


def test_every_v1_route_requires_key(api_client, monkeypatch):
    client, _headers = api_client
    # Keep the seams mocked so an authenticated call would succeed (we only test
    # the 401 path here, but this guarantees the 401 is auth, not a crash).
    monkeypatch.setattr(legal.dispatch, "run_operation", lambda *a, **k: _ok_envelope())
    monkeypatch.setattr(
        legal.global_search, "run_global_search", lambda *a, **k: _ok_envelope("global", "search")
    )

    routes = _v1_routes(client.app)
    assert routes, "expected at least one /v1 route"
    for method, path in routes:
        body: dict[str, Any] = {"text": "x"} if path.endswith("/search") else {}
        resp = client.request(method, path, json=body)  # no auth header
        assert resp.status_code == 401, f"{method} {path} should be 401 without a key"


def test_healthz_open_without_key(api_client):
    client, _headers = api_client
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_v1_route_succeeds_with_key(api_client, monkeypatch):
    client, headers = api_client
    monkeypatch.setattr(
        legal.dispatch, "run_operation", lambda *a, **k: _ok_envelope("saij", "search")
    )
    resp = client.post("/v1/sources/saij/search", json={"params": {"text": "hola"}}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_v1_route_rejects_wrong_key(api_client):
    client, _headers = api_client
    resp = client.post(
        "/v1/sources/saij/search",
        json={"params": {}},
        headers={"x-api-key": "wrong-key"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# 2. Error mapping: envelope code -> HTTP status; body is always the envelope
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "status"),
    [("not_found", 404), ("source_unavailable", 502)],
)
def test_error_envelope_maps_to_status(api_client, monkeypatch, code, status):
    client, headers = api_client
    monkeypatch.setattr(
        legal.dispatch,
        "run_operation",
        lambda *a, **k: _error_envelope(code),
    )
    resp = client.post("/v1/sources/saij/search", json={"params": {}}, headers=headers)
    assert resp.status_code == status
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == code


def test_raised_usage_error_maps_to_400(api_client, monkeypatch):
    client, headers = api_client

    def _raise(*_a, **_k):
        raise usage_error("bad params", details={"why": "test"})

    monkeypatch.setattr(legal.dispatch, "run_operation", _raise)
    resp = client.post("/v1/sources/saij/search", json={"params": {}}, headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "usage_error"
    # Body is the normalized envelope, not FastAPI's {"detail": ...}.
    assert "detail" not in body
    assert body["source"] == "saij"
    assert body["operation"] == "search"


def test_ok_envelope_maps_to_200(api_client, monkeypatch):
    client, headers = api_client
    monkeypatch.setattr(legal.dispatch, "run_operation", lambda *a, **k: _ok_envelope())
    resp = client.post("/v1/sources/saij/search", json={"params": {}}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --------------------------------------------------------------------------- #
# 3. Generic-route coverage over the whole registry
# --------------------------------------------------------------------------- #


def test_generic_route_reaches_run_operation_for_every_source(api_client, monkeypatch):
    client, headers = api_client

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _capture(source_id, operation, params=None, **_kwargs):
        params = dict(params or {})
        calls.append((source_id, operation, params))
        return _ok_envelope(source_id, operation)

    monkeypatch.setattr(legal.dispatch, "run_operation", _capture)

    sources = legal.registry.list_sources()
    assert sources, "expected a non-empty registry"

    covered = 0
    for source in sources:
        source_id = source["id"]
        op = _first_supported_op(source)
        assert op is not None, f"{source_id} has no supported operation"
        resp = client.post(
            f"/v1/sources/{source_id}/{op}",
            json={"params": {"probe": source_id}},
            headers=headers,
        )
        assert resp.status_code == 200, f"{source_id}/{op} -> {resp.status_code}"
        body = resp.json()
        assert body["ok"] is True
        assert body["source"] == source_id
        assert body["operation"] == op
        covered += 1

    assert covered == len(sources)
    # Every source/op pair actually reached the mocked dispatch seam.
    reached = {(s, o) for s, o, _ in calls}
    for source in sources:
        op = _first_supported_op(source)
        assert (source["id"], op) in reached


def test_generic_route_threads_params_and_raw(api_client, monkeypatch):
    client, headers = api_client
    seen: dict[str, Any] = {}

    def _capture(source_id, operation, params=None, **_kwargs):
        seen["args"] = (source_id, operation, dict(params or {}))
        return _ok_envelope(source_id, operation)

    monkeypatch.setattr(legal.dispatch, "run_operation", _capture)
    resp = client.post(
        "/v1/sources/saij/search",
        json={"params": {"text": "hola"}, "raw": True},
        headers=headers,
    )
    assert resp.status_code == 200
    source_id, operation, params = seen["args"]
    assert (source_id, operation) == ("saij", "search")
    assert params["text"] == "hola"
    assert params["raw"] is True


# --------------------------------------------------------------------------- #
# 4. Discovery shapes
# --------------------------------------------------------------------------- #


def test_discovery_list_sources(api_client):
    client, headers = api_client
    resp = client.get("/v1/sources", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    expected_ids = {s["id"] for s in legal.registry.list_sources()}
    got_ids = {item["id"] for item in body["items"]}
    assert got_ids == expected_ids


def test_discovery_get_source(api_client):
    client, headers = api_client
    resp = client.get("/v1/sources/saij", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "saij"
    assert "operations" in body


def test_discovery_get_unknown_source_returns_envelope(api_client):
    client, headers = api_client
    resp = client.get("/v1/sources/does-not-exist", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_discovery_schema(api_client):
    client, headers = api_client
    resp = client.get("/v1/schema", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == legal.schema.LEGAL_RESPONSE_SCHEMA


# --------------------------------------------------------------------------- #
# 5. Typed routes map to the right (source, operation) + thread body params
# --------------------------------------------------------------------------- #


def test_typed_csjn_fallos(api_client, monkeypatch):
    client, headers = api_client
    seen: dict[str, Any] = {}

    def _capture(source_id, operation, params=None, **_kwargs):
        seen["args"] = (source_id, operation, dict(params or {}))
        return _ok_envelope(source_id, operation)

    monkeypatch.setattr(legal.dispatch, "run_operation", _capture)
    resp = client.post(
        "/v1/csjn/fallos",
        json={"texto": "amparo", "limit": 3},
        headers=headers,
    )
    assert resp.status_code == 200
    source_id, operation, params = seen["args"]
    assert (source_id, operation) == ("csjn", "fallos")
    assert params["texto"] == "amparo"
    assert params["limit"] == 3
    # exclude_none drops unset fields.
    assert "partes" not in params


def test_typed_csjn_sumarios_forwards_citation(api_client, monkeypatch):
    client, headers = api_client
    seen: dict[str, Any] = {}

    def _capture(source_id, operation, params=None, **_kwargs):
        seen["args"] = (source_id, operation, dict(params or {}))
        return _ok_envelope(source_id, operation)

    monkeypatch.setattr(legal.dispatch, "run_operation", _capture)
    resp = client.post(
        "/v1/csjn/sumarios",
        json={"tomo": "327", "pagina": "3753", "limit": 5},
        headers=headers,
    )
    assert resp.status_code == 200
    source_id, operation, params = seen["args"]
    assert (source_id, operation) == ("csjn", "sumarios")
    # the Fallos-citation fields must reach the pipeline, not be dropped
    assert params["tomo"] == "327"
    assert params["pagina"] == "3753"
    assert "texto" not in params  # exclude_none drops unset fields


def test_typed_saij_search(api_client, monkeypatch):
    client, headers = api_client
    seen: dict[str, Any] = {}

    def _capture(source_id, operation, params=None, **_kwargs):
        seen["args"] = (source_id, operation, dict(params or {}))
        return _ok_envelope(source_id, operation)

    monkeypatch.setattr(legal.dispatch, "run_operation", _capture)
    resp = client.post(
        "/v1/saij/search",
        json={"text": "habeas corpus", "limit": 5},
        headers=headers,
    )
    assert resp.status_code == 200
    source_id, operation, params = seen["args"]
    assert (source_id, operation) == ("saij", "search")
    assert params["text"] == "habeas corpus"
    assert params["limit"] == 5
    assert "raw_query" not in params


def test_typed_routes_appear_in_openapi(api_client):
    client, headers = api_client
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/v1/csjn/fallos" in paths
    assert "/v1/saij/search" in paths


# --------------------------------------------------------------------------- #
# 6. Global search
# --------------------------------------------------------------------------- #


def test_global_search_all_direct_calls_core(api_client, monkeypatch):
    client, headers = api_client
    seen: dict[str, Any] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _ok_envelope("global", "search")

    monkeypatch.setattr(legal.global_search, "run_global_search", _capture)
    resp = client.post(
        "/v1/search",
        json={"text": "amparo", "all_direct": True, "limit_per_source": 2},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen["text"] == "amparo"
    assert seen["all_direct"] is True
    assert seen["limit_per_source"] == 2


def test_global_search_missing_selector_is_usage_error(api_client, monkeypatch):
    client, headers = api_client

    def _raise(**_kwargs):
        raise usage_error("select sources via --all-direct or --sources")

    monkeypatch.setattr(legal.global_search, "run_global_search", _raise)
    resp = client.post("/v1/search", json={"text": "amparo"}, headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "usage_error"
    assert "detail" not in body
