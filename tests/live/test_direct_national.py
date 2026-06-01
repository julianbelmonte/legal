"""Live tests — national/federal direct HTTP sources.

These sources need neither a browser nor captcha, so they are the cheapest live
coverage and exercise the relocation + proxy-disabled egress path against the
real sites. Every test is gated behind ``LEGAL_LIVE=1`` (root ``conftest``) and
uses the in-process :func:`dispatch` seam with a minimal real query and a small
``limit`` to keep load light. Each call must return a normalized ``ok`` envelope
(see :func:`assert_ok_envelope`); an ``ok:false`` envelope surfaces
``error.code``/``error.message`` so a real regression is immediately actionable.

The national/federal direct group is: ``saij``, ``infoleg``, ``bo-nacional``,
``bcra``, ``pjn-juris``, ``cnacaf``, ``aaip``, ``tfn``. Flags below were
confirmed against ``legal/sources/<id>.py`` and ``legal/docs/``.
"""

from __future__ import annotations

import pytest

from tests.live._helpers import assert_ok_envelope, dispatch

pytestmark = pytest.mark.live


def test_saij_search() -> None:
    """SAIJ free-text search (``--text`` mapped to texto)."""
    env = dispatch("saij", "search", text="despido", limit=3)
    assert_ok_envelope(env)


def test_infoleg_search() -> None:
    """Infoleg free-text search."""
    env = dispatch("infoleg", "search", text="ley 26076", limit=3)
    assert_ok_envelope(env)


def test_bo_nacional_filters() -> None:
    """Boletin Oficial Nacional filter discovery (cheap, no query)."""
    env = dispatch("bo-nacional", "filters")
    assert_ok_envelope(env)


def test_bo_nacional_search() -> None:
    """Boletin Oficial Nacional advanced search (``--section`` + ``--keywords``)."""
    env = dispatch(
        "bo-nacional",
        "search",
        section="primera",
        keywords="resolucion",
        limit=3,
    )
    assert_ok_envelope(env)


def test_bcra_filters() -> None:
    """BCRA filter discovery (cheap, no query)."""
    env = dispatch("bcra", "filters")
    assert_ok_envelope(env)


def test_bcra_search() -> None:
    """BCRA index search (``--text`` mapped to q)."""
    env = dispatch("bcra", "search", text="tasa", limit=3)
    assert_ok_envelope(env)


def test_pjn_juris_search() -> None:
    """PJN Jurisprudencia search (``--terms`` free text)."""
    env = dispatch("pjn-juris", "search", terms="danos", limit=3)
    assert_ok_envelope(env)


def test_cnacaf_filters() -> None:
    """CNACAF filter discovery (cheap, no query)."""
    env = dispatch("cnacaf", "filters")
    assert_ok_envelope(env)


def test_cnacaf_search() -> None:
    """CNACAF search through the TFN/CNCAF API (``--query`` free text)."""
    env = dispatch("cnacaf", "search", query="honorarios", limit=3)
    assert_ok_envelope(env)


def test_aaip_search() -> None:
    """AAIP search over the public sheet (``search`` auto-fetches the sheet)."""
    env = dispatch("aaip", "search", text="datos", limit=3)
    assert_ok_envelope(env)


def test_tfn_filters() -> None:
    """TFN filter discovery (cheap, no query)."""
    env = dispatch("tfn", "filters")
    assert_ok_envelope(env)


def test_tfn_latest() -> None:
    """TFN latest API cases."""
    env = dispatch("tfn", "latest", limit=3)
    assert_ok_envelope(env)
