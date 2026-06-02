"""Offline HTTP-level tests for the Cloudzy client.

These tests drive :class:`legal_deploy.cloudzy.CloudzyClient` through a mocked
``httpx`` transport (``httpx.MockTransport``) so they assert request
construction (method, URL, the ``API-Token`` auth header) and OKAY/FAILED
envelope normalization WITHOUT contacting the real Cloudzy API. No real token
and no network are ever used.
"""

from __future__ import annotations

import httpx
import pytest

from legal_deploy.cloudzy import (
    CloudzyClient,
    CloudzyError,
    CloudzyTimeoutError,
    CreateInstanceRequest,
)

DUMMY_TOKEN = "test-token-not-a-real-secret"


def _client(handler) -> CloudzyClient:
    """Build a CloudzyClient backed by a mocked transport and dummy token."""
    http_client = httpx.Client(
        base_url="https://api.cloudzy.test/developers",
        transport=httpx.MockTransport(handler),
    )
    return CloudzyClient(
        base_url="https://api.cloudzy.test/developers",
        token=DUMMY_TOKEN,
        client=http_client,
    )


# -- construction / auth -----------------------------------------------------


def test_missing_token_raises_without_network(monkeypatch):
    monkeypatch.delenv("CLOUDZY_API_TOKEN", raising=False)
    with pytest.raises(CloudzyError):
        CloudzyClient()


def test_token_resolved_from_env(monkeypatch):
    monkeypatch.setenv("CLOUDZY_API_TOKEN", "env-token-value")
    client = CloudzyClient()
    assert client.headers()["API-Token"] == "env-token-value"


def test_token_not_exposed_in_repr():
    client = CloudzyClient(token=DUMMY_TOKEN)
    assert DUMMY_TOKEN not in repr(client)


# -- request construction (method / url / headers) ---------------------------


def test_get_request_carries_method_url_and_auth_header():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("API-Token")
        seen["accept"] = request.headers.get("Accept")
        return httpx.Response(200, json={"status": "OKAY", "data": [{"id": "r1"}]})

    with _client(handler) as client:
        data = client.list_regions()

    assert seen["method"] == "GET"
    assert seen["path"] == "/developers/v1/regions"
    assert seen["token"] == DUMMY_TOKEN
    assert seen["accept"] == "application/json"
    assert data == [{"id": "r1"}]


def test_create_instance_posts_payload():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={"status": "OKAY", "data": {"id": "i-1"}})

    request = CreateInstanceRequest(
        region="US-Las-Vegas",
        product="prod-uuid",
        operating_system="os-id",
        ssh_keys=["12362"],
        hostname="legal-agent",
    )
    with _client(handler) as client:
        data = client.create_instance(request)

    assert seen["method"] == "POST"
    assert seen["path"] == "/developers/v1/instances"
    # The live API uses these field names; ssh key ids are coerced to ints.
    assert '"region"' in seen["body"]
    assert '"hostnames"' in seen["body"]
    assert '"productId"' in seen["body"]
    assert '"osId"' in seen["body"]
    assert '"sshKeyIds"' in seen["body"]
    assert "[12362]" in seen["body"].replace(" ", "")
    assert data == {"id": "i-1"}


def test_destroy_instance_uses_delete():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "OKAY", "data": {"deleted": True}})

    with _client(handler) as client:
        data = client.destroy_instance("i-42")

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/developers/v1/instances/i-42"
    assert data == {"deleted": True}


def test_create_instance_rejects_request_and_kwargs():
    with _client(lambda r: httpx.Response(200, json={"status": "OKAY"})) as client:
        with pytest.raises(CloudzyError):
            client.create_instance(
                CreateInstanceRequest(region="us", product="p"), region="x"
            )


# -- envelope normalization --------------------------------------------------


def test_okay_envelope_returns_inner_data():
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"status": "OKAY", "data": {"a": 1}}
    )
    with _client(handler) as client:
        assert client.list_products() == {"a": 1}


def test_okay_envelope_without_data_returns_body():
    handler = lambda r: httpx.Response(200, json={"status": "OKAY", "x": 9})  # noqa: E731
    with _client(handler) as client:
        assert client.list_products() == {"status": "OKAY", "x": 9}


def test_failed_envelope_raises_cloudzy_error():
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"status": "FAILED", "message": "bad request"}
    )
    with _client(handler) as client:
        with pytest.raises(CloudzyError) as exc:
            client.list_instances()
    assert "bad request" in str(exc.value)


def test_unrecognized_status_body_passes_through():
    handler = lambda r: httpx.Response(200, json={"foo": "bar"})  # noqa: E731
    with _client(handler) as client:
        assert client.list_regions() == {"foo": "bar"}


# -- HTTP / transport errors -------------------------------------------------


def test_http_error_status_raises_with_status_code():
    handler = lambda r: httpx.Response(  # noqa: E731
        500, json={"status": "FAILED", "message": "boom"}
    )
    with _client(handler) as client:
        with pytest.raises(CloudzyError) as exc:
            client.list_regions()
    assert exc.value.status_code == 500


def test_transport_request_error_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(handler) as client:
        with pytest.raises(CloudzyError):
            client.list_regions()


# -- readiness polling -------------------------------------------------------


def test_wait_for_instance_returns_on_ready_state():
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"status": "OKAY", "data": {"id": "i-1", "state": "RUNNING"}}
    )
    with _client(handler) as client:
        ready = client.wait_for_instance("i-1", timeout=5.0, interval=0.0)
    assert ready["state"] == "RUNNING"


def test_wait_for_instance_raises_on_failed_state():
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"status": "OKAY", "data": {"id": "i-1", "state": "ERROR"}}
    )
    with _client(handler) as client:
        with pytest.raises(CloudzyError):
            client.wait_for_instance("i-1", timeout=5.0, interval=0.0)


def test_wait_for_instance_times_out():
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"status": "OKAY", "data": {"id": "i-1", "state": "PENDING"}}
    )
    with _client(handler) as client:
        with pytest.raises(CloudzyTimeoutError):
            client.wait_for_instance("i-1", timeout=0.0, interval=0.0)
