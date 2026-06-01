"""Live tests — provincial / CABA direct HTTP sources.

These sources need neither a browser nor captcha, so they are cheap live
coverage and exercise the relocation + proxy-disabled egress path against the
real sites. Every test is gated behind ``LEGAL_LIVE=1`` (root ``conftest``) and
uses the in-process :func:`dispatch` seam with a minimal real query and a small
``limit`` to keep load light. Each call must return a normalized ``ok`` envelope
(see :func:`assert_ok_envelope`); an ``ok:false`` envelope surfaces
``error.code``/``error.message`` so a real regression is immediately actionable.

The provincial/CABA direct group is: ``normas-pba``, ``bo-pba``, ``juba``,
``jusbaires``, ``dppj``, ``igj``. Together with the national group (Step 34) this
covers every non-browser source. Flags below were confirmed against
``legal/sources/<id>.py`` and ``legal/docs/``.
"""

from __future__ import annotations

import pytest

from tests.live._helpers import assert_ok_envelope, dispatch

pytestmark = pytest.mark.live


def test_normas_pba_search() -> None:
    """Normas PBA free-text search (``--text`` exact phrase)."""
    env = dispatch("normas-pba", "search", text="sociedad", limit=3)
    assert_ok_envelope(env)


def test_bo_pba_search() -> None:
    """Boletin Oficial PBA search (``--section`` OFICIAL + ``--words``).

    ``--from``/``--to`` are omitted: their argparse dest is ``date_from`` /
    ``date_to`` but the flag spelling is the python reserved word ``from`` which
    the in-process ``**params`` helper cannot pass; the bare word search is a
    valid minimal query (default date window).
    """
    env = dispatch("bo-pba", "search", section="OFICIAL", words="ministerio", limit=3)
    assert_ok_envelope(env)


def test_juba_search() -> None:
    """JUBA WebForms free-text quick search (``--text`` mapped to text)."""
    env = dispatch("juba", "search", text="amparo", limit=3)
    assert_ok_envelope(env)


def test_jusbaires_search() -> None:
    """Jusbaires (Juristeca) fallos search (``--text`` required term)."""
    env = dispatch("jusbaires", "search", text="amparo", limit=3)
    assert_ok_envelope(env)


def test_dppj_list() -> None:
    """DPPJ official legislation link listing (cheap, no query)."""
    env = dispatch("dppj", "list", limit=3)
    assert_ok_envelope(env)


def test_igj_list() -> None:
    """IGJ official yearly resolution listing (``--year``)."""
    env = dispatch("igj", "list", year=2026, limit=3)
    assert_ok_envelope(env)


def test_igj_search() -> None:
    """IGJ search via SAIJ (``--text`` mapped to texto, IGJ facet enforced)."""
    env = dispatch("igj", "search", text="sociedades", limit=3)
    assert_ok_envelope(env)
