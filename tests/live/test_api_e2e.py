"""Live tests — FastAPI end-to-end against **real** source handlers.

Step 32 mocked the dispatch seam to prove the API's routing/auth/error-mapping
in isolation. This module instead exercises the *full* path with no mocks:

    TestClient -> router -> run_in_threadpool -> run_operation -> real source

proving the HTTP surface is truly 1:1 with the CLI. Every test is ``live``
(skipped unless ``LEGAL_LIVE=1``; see the root ``conftest``) and uses the
authenticated ``api_client`` fixture (a configured ``TestClient`` plus the
matching ``x-api-key`` header).

Coverage is intentionally small to keep cost low and stays clear of the
Capsolver-spending operations entirely:

* ``POST /v1/sources/saij/search`` — generic uniform route, direct HTTP source.
* ``POST /v1/saij/search`` — the typed SAIJ route, same query.
* ``POST /v1/search`` — global cross-source fan-out (``all_direct``).
* ``POST /v1/csjn/fallos`` — typed CSJN route (free browser scoring, tolerant of
  the too-broad/refine warning), the only browser-backed case here.

One case also compares an API envelope's top-level shape against the equivalent
CLI invocation for the same query, asserting the 1:1 contract holds end to end.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from tests.live._helpers import assert_ok_envelope, cli

pytestmark = pytest.mark.live

#: Extra attempts for CSJN's probabilistic reCAPTCHA Enterprise score gate.
FALLOS_RETRIES = 3


def _post(api_client: tuple[Any, dict[str, str]], path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    """POST ``body`` to ``path`` with auth and return the JSON envelope.

    Asserts an HTTP 200 (the normalized envelope is always returned with a 200
    for ``ok:true`` responses) and that the body decodes to a mapping.
    """
    client, headers = api_client
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, (
        f"POST {path} -> HTTP {response.status_code}: {response.text!r}"
    )
    env = response.json()
    assert isinstance(env, Mapping), f"non-mapping envelope from {path}: {env!r}"
    return env


def test_generic_saij_search(api_client: tuple[Any, dict[str, str]]) -> None:
    """``POST /v1/sources/saij/search`` hits the real SAIJ handler.

    Exercises the generic uniform route end to end and asserts a normalized
    ``ok`` envelope echoing ``source=="saij"`` / ``operation=="search"``.
    """
    env = _post(
        api_client,
        "/v1/sources/saij/search",
        {"params": {"text": "despido", "limit": 2}},
    )
    env = assert_ok_envelope(env)
    assert env.get("source") == "saij", env
    assert env.get("operation") == "search", env


def test_typed_saij_search(api_client: tuple[Any, dict[str, str]]) -> None:
    """``POST /v1/saij/search`` (typed body) hits the real SAIJ handler.

    Same query as the generic route; the typed endpoint must return the same
    normalized envelope shape (1:1 with the CLI / generic route).
    """
    env = _post(api_client, "/v1/saij/search", {"text": "despido", "limit": 2})
    env = assert_ok_envelope(env)
    assert env.get("source") == "saij", env
    assert env.get("operation") == "search", env


def test_global_search_all_direct(api_client: tuple[Any, dict[str, str]]) -> None:
    """``POST /v1/search`` fans the query out across all direct sources.

    Asserts a normalized ``ok`` envelope with ``operation=="search"`` and an
    aggregated ``items`` list (the cross-source result rows).
    """
    env = _post(
        api_client,
        "/v1/search",
        {"text": "ley 26076", "all_direct": True, "limit_per_source": 1},
    )
    env = assert_ok_envelope(env)
    assert env.get("operation") == "search", env
    assert isinstance(env.get("items"), list), env


def test_api_cli_envelope_parity(api_client: tuple[Any, dict[str, str]]) -> None:
    """The API envelope is 1:1 with the CLI envelope for the same query.

    Drives ``saij search`` through both the typed HTTP endpoint and the CLI
    subprocess and asserts their top-level envelope shapes match: same set of
    top-level keys and the same ``ok``/``source``/``operation`` scalars. The
    per-item result rows differ run to run (live data), so only the contract
    (envelope shape), not the payload, is compared.
    """
    api_env = _post(api_client, "/v1/saij/search", {"text": "despido", "limit": 2})
    api_env = assert_ok_envelope(api_env)

    cli_env = cli("saij", "search", "--text", "despido", "--limit", "2")
    cli_env = assert_ok_envelope(cli_env)

    assert set(api_env.keys()) == set(cli_env.keys()), (
        "API/CLI top-level envelope keys diverge: "
        f"api-only={set(api_env) - set(cli_env)!r} cli-only={set(cli_env) - set(api_env)!r}"
    )
    for key in ("ok", "source", "operation"):
        assert api_env.get(key) == cli_env.get(key), (
            f"API/CLI envelope scalar {key!r} diverges: "
            f"api={api_env.get(key)!r} cli={cli_env.get(key)!r}"
        )


def test_typed_csjn_fallos(api_client: tuple[Any, dict[str, str]]) -> None:
    """``POST /v1/csjn/fallos`` drives the real (free) browser-backed handler.

    CSJN uses native reCAPTCHA Enterprise scoring (no Capsolver spend) and is
    probabilistic, so a generous ``retries`` is supplied. A broad ``texto`` may
    legitimately exceed CSJN's row cap and return the refine/narrow-query warning
    with empty items — that is an accepted search (``ok:true``), not a failure.
    """
    env = _post(
        api_client,
        "/v1/csjn/fallos",
        {"texto": "arbitrariedad", "limit": 2, "retries": FALLOS_RETRIES},
    )
    env = assert_ok_envelope(env)
    assert env.get("source") == "csjn", env
    assert env.get("operation") == "fallos", env
