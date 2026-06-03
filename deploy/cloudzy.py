"""HTTP client for the Cloudzy Developer API.

The Cloudzy Developer API is rooted at ``/developers`` and authenticates with an
``API-Token`` header. The concrete v1 resources live under ``/developers/v1/*``
(``/v1/regions``, ``/v1/products``, ``/v1/os``, ``/v1/ssh-keys``,
``/v1/instances``). Successful responses wrap their payload as
``{"code": "OKAY", "detail": <message>, "data": <payload>}``; this client
normalizes that envelope, returning the inner ``data`` payload on success and
raising :class:`CloudzyError` on failure.

The request/response field names here match the live Cloudzy Developer API
OpenAPI schema (validated against ``/developers/openapi.json``): create-instance
takes ``hostnames`` (array), ``productId``, ``osId``/``osName``, ``sshKeyIds``
(integers), ``region``, ``appId`` and ``billingCycle``; ``products`` requires a
``regionId`` query param; operating systems are at ``/v1/os`` with the list under
``data.os``; ssh keys are under ``data.sshKeys``.

This module is standalone deploy tooling. It uses ``httpx`` directly and does not
import from the legal pipeline's source-access internals.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.cloudzy.com/developers"
#: The live v1 resources live under ``/developers/v1/*``.
API_PREFIX = "/v1"
DEFAULT_TIMEOUT = 30.0
TOKEN_ENV_VAR = "CLOUDZY_API_TOKEN"

#: Cloudzy result sentinels. The live API reports the result of a call in the
#: ``code`` field of the envelope (``OKAY`` on a read, ``CREATED`` on a create).
#: ``status`` is accepted too for forward/backward compatibility.
STATUS_OKAY = "OKAY"
STATUS_FAILED = "FAILED"
#: Result codes that indicate success (the inner ``data`` should be returned).
SUCCESS_CODES = {"OKAY", "CREATED", "ACCEPTED"}

# Instance lifecycle states that count as "ready" when polling for readiness.
# The live API reports instance state in the ``status`` field; an instance that
# finished provisioning reports ``running``/``active``. Comparison is
# case-insensitive (see ``_instance_state`` callers).
READY_STATES = {"RUNNING", "ACTIVE", "OKAY", "READY", "ONLINE", "POWERON"}
# Terminal failure states that should stop a readiness poll early.
FAILED_STATES = {"FAILED", "ERROR", "TERMINATED", "DELETED"}


class CloudzyError(RuntimeError):
    """Raised when the Cloudzy API returns a FAILED envelope or an HTTP error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class CloudzyTimeoutError(CloudzyError):
    """Raised when a readiness poll exceeds its timeout."""


@dataclass
class CreateInstanceRequest:
    """Typed payload for :meth:`CloudzyClient.create_instance`.

    Field names follow the deploy-facing convention (``product``,
    ``operating_system``, ``hostname``, ``ssh_keys``); :meth:`to_payload`
    translates them to the live Cloudzy API names (``productId``, ``osId``,
    ``hostnames``, ``sshKeyIds``). ``region`` and at least one hostname are
    required by the API. ``ssh_keys`` are Cloudzy SSH key **ids** (integers) and
    are coerced to ints in the payload.
    """

    region: str
    product: str
    operating_system: str | None = None
    application: str | None = None
    hostname: str | None = None
    ssh_keys: list[str] = field(default_factory=list)
    label: str | None = None
    billing_cycle: str = "hourly"
    #: The API rejects a create with neither IPv4 nor IPv6 selected
    #: (``ONE_OF_IPV4_OR_IPV6_MUST_BE_SELECTED``); default to IPv4.
    assign_ipv4: bool = True
    assign_ipv6: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        # The API requires at least one hostname; default to the instance label
        # or a stable fallback so a request is always well-formed.
        hostname = self.hostname or self.label or "legal-agent"
        payload: dict[str, Any] = {
            "region": self.region,
            "hostnames": [hostname],
            "billingCycle": self.billing_cycle,
            "assignIpv4": self.assign_ipv4,
            "assignIpv6": self.assign_ipv6,
        }
        if self.product:
            payload["productId"] = self.product
        if self.operating_system is not None:
            payload["osId"] = self.operating_system
        if self.application is not None:
            payload["appId"] = self.application
        if self.ssh_keys:
            payload["sshKeyIds"] = [_as_ssh_key_id(k) for k in self.ssh_keys]
        payload.update(self.extra)
        return payload


def _as_ssh_key_id(value: Any) -> Any:
    """Coerce an SSH key id to int when it looks numeric (the API wants ints)."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


class CloudzyClient:
    """Sync HTTP client for the Cloudzy Developer API.

    The token is resolved from the ``token`` argument or the ``CLOUDZY_API_TOKEN``
    environment variable. It is stored privately and never exposed via ``repr`` or
    logging.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_token = token if token is not None else os.environ.get(TOKEN_ENV_VAR)
        if not resolved_token:
            raise CloudzyError(
                "no Cloudzy API token provided; pass token= or set "
                f"{TOKEN_ENV_VAR}"
            )
        self.base_url = base_url.rstrip("/")
        self._token = resolved_token
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:  # pragma: no cover - trivial, token-safe
        return f"CloudzyClient(base_url={self.base_url!r})"

    # -- auth / transport ------------------------------------------------

    def headers(self) -> dict[str, str]:
        """Return request headers including the ``API-Token`` auth header."""
        return {
            "API-Token": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "CloudzyClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Run a request and normalize the Cloudzy OKAY/FAILED envelope."""
        try:
            response = self._http().request(
                method,
                self._url(path),
                headers=self.headers(),
                json=json,
                params=params,
            )
        except httpx.RequestError as exc:
            raise CloudzyError(f"Cloudzy request failed: {exc}") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            payload = _safe_json(response)
            raise CloudzyError(
                f"Cloudzy HTTP {response.status_code} for {method} {path}",
                status_code=response.status_code,
                payload=payload,
            ) from exc

        body = _safe_json(response)
        return _normalize_envelope(body)

    # -- read operations -------------------------------------------------

    def list_regions(self) -> Any:
        return self._request("GET", f"{API_PREFIX}/regions")

    def list_products(self, region_id: str | None = None) -> Any:
        # The live API requires a ``regionId`` query param to list products.
        params = {"regionId": region_id} if region_id else None
        return self._request("GET", f"{API_PREFIX}/products", params=params)

    def list_operating_systems(self) -> Any:
        return self._request("GET", f"{API_PREFIX}/os")

    #: Alias used by some deploy steps.
    list_os_images = list_operating_systems

    def list_applications(self) -> Any:
        return self._request("GET", f"{API_PREFIX}/applications")

    def list_ssh_keys(self) -> Any:
        return self._request("GET", f"{API_PREFIX}/ssh-keys")

    def list_instances(self) -> Any:
        return self._request("GET", f"{API_PREFIX}/instances")

    def get_instance(self, instance_id: str) -> Any:
        return self._request("GET", f"{API_PREFIX}/instances/{instance_id}")

    # -- write operations ------------------------------------------------

    def create_instance(
        self,
        request: CreateInstanceRequest | None = None,
        **kwargs: Any,
    ) -> Any:
        """Create an instance from a :class:`CreateInstanceRequest` or kwargs."""
        if request is None:
            request = CreateInstanceRequest(**kwargs)
        elif kwargs:
            raise CloudzyError("pass either a request object or kwargs, not both")
        return self._request(
            "POST", f"{API_PREFIX}/instances", json=request.to_payload()
        )

    def destroy_instance(self, instance_id: str) -> Any:
        return self._request("DELETE", f"{API_PREFIX}/instances/{instance_id}")

    # -- readiness -------------------------------------------------------

    def wait_for_instance(
        self,
        instance_id: str,
        *,
        timeout: float = 600.0,
        interval: float = 10.0,
    ) -> Any:
        """Poll an instance until it reaches a ready state or times out."""
        deadline = time.monotonic() + timeout
        while True:
            data = self.get_instance(instance_id)
            state = _instance_state(data)
            if state is not None:
                upper = state.upper()
                if upper in READY_STATES:
                    return data
                if upper in FAILED_STATES:
                    raise CloudzyError(
                        f"instance {instance_id} entered failed state {state!r}",
                        payload=data,
                    )
            if time.monotonic() >= deadline:
                raise CloudzyTimeoutError(
                    f"instance {instance_id} not ready within {timeout}s",
                    payload=data,
                )
            time.sleep(interval)

    # Alias for callers that prefer the poll_* naming.
    poll_instance_ready = wait_for_instance


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _normalize_envelope(body: Any) -> Any:
    """Normalize a Cloudzy result envelope to its data payload.

    The live API reports the result in ``code`` (``"OKAY"`` on success); older
    shapes used ``status``. On success returns the ``data`` payload (or the full
    body when no ``data`` key is present). On an explicit failure code raises
    :class:`CloudzyError`. Bodies without a recognized result field are returned
    unchanged.
    """
    if not isinstance(body, dict):
        return body
    result = body.get("code", body.get("status"))
    if result == STATUS_FAILED:
        message = (
            body.get("detail")
            or body.get("message")
            or body.get("error")
            or "Cloudzy request failed"
        )
        raise CloudzyError(str(message), payload=body)
    if result in SUCCESS_CODES:
        return body.get("data", body)
    return body


def _instance_state(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("status", "state", "power_status", "powerStatus"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _find_instance_id(node: Any) -> str | None:
    """Recursively find an instance id in a (possibly nested) create response.

    The live create-instance response nests the created instance(s) under
    ``data.instances`` and each entry may itself be a ``{code, detail, data}``
    envelope. This walks dicts/lists and returns the first plausible instance
    id (a UUID-shaped ``id``/``instanceId``).
    """
    if isinstance(node, dict):
        for key in ("id", "instanceId", "instance_id", "uuid"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in node.values():
            found = _find_instance_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_instance_id(item)
            if found:
                return found
    return None


def created_instance_id(create_data: Any) -> str | None:
    """Return the created instance id from a create-instance ``data`` payload."""
    return _find_instance_id(create_data)
