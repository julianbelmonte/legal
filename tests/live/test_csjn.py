"""Live tests — CSJN (browser-backed, native reCAPTCHA Enterprise scoring).

CSJN is the only browser-backed source that does **not** spend Capsolver credits:
it drives BotBrowser (under a hidden Xvfb) and relies on native reCAPTCHA
Enterprise scoring, which is *probabilistic*. The adapter exposes a ``--retries``
flag because a single attempt may be rejected by the score gate. A broad query
can also legitimately return zero result rows with a "refine your query" notice
when the match exceeds CSJN's 5000-row server cap — that is ``ok:true`` (an
accepted search), **not** a failure.

These tests validate the relocated ``legal/browser.py`` + the vendored BotBrowser
binary/profiles + the multi-profile ``config.pick_profile`` change actually
launch and parse against the real site. Gating: every test is ``live`` (skipped
unless ``LEGAL_LIVE=1``; see the root ``conftest``).

Success/failure policy for ``csjn fallos``:

* ``ok:true`` with items                          -> pass (full search + parse).
* ``ok:true`` with the narrow-query warning + []  -> pass (accepted, too broad).
* ``ok:false`` (browser launch / score / parse)   -> fail (real regression or a
  transient source-state rejection after retries).

Because the score gate is probabilistic, the fallos search uses a generous
``retries`` so a single unlucky score does not flake the suite; a persistent
``ok:false`` still surfaces ``error.code``/``error.message`` so a genuine
relocation regression (e.g. the browser failing to launch) is actionable.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live._helpers import assert_ok_envelope, dispatch

pytestmark = pytest.mark.live

#: Extra attempts for the probabilistic reCAPTCHA Enterprise score gate.
FALLOS_RETRIES = 6


def _first_doc_id(env: Mapping[str, object]) -> str | None:
    """Return the first result's CSJN ``idDocumento`` (``doc_id``), if any."""
    items = env.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        source_fields = item.get("source_fields")
        if isinstance(source_fields, Mapping):
            doc_id = source_fields.get("doc_id")
            if isinstance(doc_id, str) and doc_id.isdigit():
                return doc_id
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id.isdigit():
            return item_id
    return None


def test_csjn_fallos_search() -> None:
    """CSJN fallos browser search; accepts items OR the narrow-query warning.

    Validates the relocated browser launch + profile selection + HTML parsing.
    A broad ``texto`` like "arbitrariedad" frequently trips CSJN's row cap and
    returns the refine notice (``ok:true``, empty items) — accepted as success.
    If items are returned, follow up with a ``documento`` fetch on the first hit.
    """
    env = dispatch(
        "csjn",
        "fallos",
        texto="arbitrariedad",
        limit=3,
        retries=FALLOS_RETRIES,
    )
    # ok:false here is a browser/score/parse failure (or a transient rejection
    # after all retries); the helper surfaces error.code/message for triage.
    env = assert_ok_envelope(env)

    doc_id = _first_doc_id(env)
    if doc_id is None:
        # Accepted but too-broad: assert the narrow-query warning is present so a
        # silently empty (rejected-looking) result still fails loudly.
        warnings = env.get("warnings") or []
        assert any(
            isinstance(w, str) and "refine" in w.lower() or "narrow" in w.lower()
            for w in warnings
        ), f"empty CSJN fallos result without a narrow-query warning: {env!r}"
        pytest.skip(
            "CSJN accepted the search but the query was too broad (refine notice); "
            "no document id to follow up on"
        )

    # A result row carried a document id: exercise the document path too.
    doc_env = dispatch("csjn", "documento", id=doc_id)
    assert_ok_envelope(doc_env)
