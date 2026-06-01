"""Live tests — Capsolver-backed sources (PTN, Sentencias SCBA, PJN Expedientes).

These three source families are the real proof that the relocated, rewired
``legal/captcha.py`` shim -> ``CapsolverBackend`` path works against the live
sites without changing any adapter behaviour:

* **PTN** (``ptn``): HTTP + invisible reCAPTCHA v3. ``search`` is captcha-free;
  ``download --text`` solves reCAPTCHA v3 via Capsolver (spends credits).
* **Sentencias SCBA** (``sentencias-scba``): HTTP. ``organisms`` and ``search``
  work *without* a captcha token; ``pdf``/``anonymize`` solve reCAPTCHA v3 via
  Capsolver (spends credits).
* **PJN Expedientes** (``pjn-expedientes``): BotBrowser + a visible image
  captcha. ``camaras`` is captcha-free; ``expediente``/``parte`` drive the
  browser and solve the image challenge via Capsolver (spends credits).

Cost discipline (see plan testing strategy + step 37):

* The captcha-free ops (``ptn search``, ``sentencias-scba organisms``/``search``,
  ``pjn-expedientes camaras``) run unconditionally under ``LEGAL_LIVE=1`` — they
  validate the relocated wiring for *free*.
* Every op that actually triggers a Capsolver solve is gated behind the
  ``requires_capsolver`` fixture (skips cleanly with no key) so a partial-secret
  environment still runs the free ops. Each spending test makes the **minimum**
  number of solves. The full credit-spending sweep is step 40; collection-only
  is this step's committed Acceptance, so the solves do not double-spend here.

Success/failure policy: a genuine "no results" from a source that legitimately
has none is acceptable (asserted as ``ok:true`` with zero items). A captcha
failure, parse failure, or browser-launch failure is a relocation regression and
fails loudly via :func:`assert_ok_envelope`, which surfaces
``error.code``/``error.message``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.live._helpers import assert_ok_envelope, cli, dispatch

pytestmark = pytest.mark.live

#: Extra attempts for the probabilistic PJN image-captcha solve.
PJN_RETRIES = 3


def _first_item_id(env: Mapping[str, object]) -> str | None:
    """Return the first result item's ``id`` string, if any."""
    items = env.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, Mapping):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                return item_id
    return None


# --------------------------------------------------------------------------- #
# PTN — HTTP + reCAPTCHA v3                                                    #
# --------------------------------------------------------------------------- #


def test_ptn_search_free() -> None:
    """PTN search is captcha-free: validates the relocated HTTP search path.

    A real query against the PTN API; ``ok:true`` is required (a parse/HTTP
    regression would surface here). Zero hits is acceptable but unlikely for a
    broad term, so we only assert the envelope is ok.
    """
    env = dispatch("ptn", "search", text="empleo publico", limit=3)
    assert_ok_envelope(env)


def test_ptn_download_text(requires_capsolver: str) -> None:
    """PTN protected download solves reCAPTCHA v3 via Capsolver (spends credits).

    Finds a real search hit, then downloads its ``dictamen`` with ``--text`` so
    the rewired captcha shim is exercised end-to-end. Gated by
    ``requires_capsolver`` so it skips cleanly without a key. Skips (not fails)
    when the search legitimately returns no hit to download.
    """
    search_env = dispatch("ptn", "search", text="empleo publico", limit=3)
    search_env = assert_ok_envelope(search_env)

    hit_id = _first_item_id(search_env)
    if hit_id is None:
        pytest.skip("PTN search returned no hit to download")

    # ``--type dictamen`` + ``--text`` (want_text). The download triggers the
    # internal reCAPTCHA v3 solve via the Capsolver backend.
    download_env = dispatch(
        "ptn",
        "download",
        id=hit_id,
        type="dictamen",
        text=True,
    )
    assert_ok_envelope(download_env)


# --------------------------------------------------------------------------- #
# Sentencias SCBA — HTTP, reCAPTCHA v3 only on pdf/anonymize                   #
# --------------------------------------------------------------------------- #


def test_scba_organisms_free() -> None:
    """SCBA organisms listing is captcha-free: validates the relocated HTTP path.

    Lists the organisms of the ``sentencias`` register; must be ``ok:true`` with
    at least one organism (the register is well populated). This is the free op
    the orchestrator validates live in this step.
    """
    env = dispatch("sentencias-scba", "organisms", register="sentencias")
    env = assert_ok_envelope(env)
    items = env.get("items")
    assert isinstance(items, list) and items, (
        f"SCBA sentencias register returned no organisms: {env!r}"
    )


def test_scba_search_free() -> None:
    """SCBA search is captcha-free: validates the relocated search path.

    Search requires a register, an organism, and a date window. ``--from``/
    ``--to`` map to argparse dests ``date_from``/``date_to``; because ``from`` is
    a Python keyword the in-process ``dispatch(**kwargs)`` helper cannot pass it,
    so this uses the subprocess :func:`cli` entry point (which the generic API
    route also mirrors via JSON params). A genuinely empty window is acceptable
    (``ok:true`` with zero items); a parse/HTTP failure is a regression.
    """
    organisms = assert_ok_envelope(
        dispatch("sentencias-scba", "organisms", register="sentencias")
    )
    organism_id = _first_item_id(organisms)
    if organism_id is None:
        pytest.skip("no SCBA organism available to scope the search")
    # Organism item ids are namespaced as ``sentencias-scba:organism:<id>``;
    # pass the bare organism id the source expects via --organism-id.
    bare_id = organism_id.rsplit(":", 1)[-1]

    env = cli(
        "sentencias-scba",
        "search",
        "--register",
        "sentencias",
        "--organism-id",
        bare_id,
        "--from",
        "2024-01-01",
        "--to",
        "2024-12-31",
        "--limit",
        "3",
    )
    assert_ok_envelope(env)


def test_scba_pdf_text(requires_capsolver: str) -> None:
    """SCBA protected PDF solves reCAPTCHA v3 via Capsolver (spends credits).

    Scopes a real search to obtain an ``idCodigoAcceso`` (the PDF code), then
    fetches that PDF with ``--text`` so the reCAPTCHA v3 solve runs through the
    Capsolver backend. Gated by ``requires_capsolver``. Skips (not fails) when no
    record is available in the window to download.
    """
    organisms = assert_ok_envelope(
        dispatch("sentencias-scba", "organisms", register="sentencias")
    )
    organism_id = _first_item_id(organisms)
    if organism_id is None:
        pytest.skip("no SCBA organism available to scope the search")
    bare_id = organism_id.rsplit(":", 1)[-1]

    search_env = cli(
        "sentencias-scba",
        "search",
        "--register",
        "sentencias",
        "--organism-id",
        bare_id,
        "--from",
        "2024-01-01",
        "--to",
        "2024-12-31",
        "--limit",
        "3",
    )
    search_env = assert_ok_envelope(search_env)
    code = _scba_access_code(search_env)
    if code is None:
        pytest.skip("SCBA search returned no record with an idCodigoAcceso to fetch")

    pdf_env = cli("sentencias-scba", "pdf", "--code", code, "--text")
    assert_ok_envelope(pdf_env)


def _scba_access_code(env: Mapping[str, object]) -> str | None:
    """Return the first result's ``idCodigoAcceso`` (the SCBA PDF code), if any."""
    items = env.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        source_fields = item.get("source_fields")
        if isinstance(source_fields, Mapping):
            code = source_fields.get("idCodigoAcceso") or source_fields.get("code")
            if isinstance(code, str) and code:
                return code
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            # ids are namespaced ``sentencias-scba:<code>``.
            return item_id.rsplit(":", 1)[-1]
    return None


# --------------------------------------------------------------------------- #
# PJN Expedientes — BotBrowser + image captcha                                #
# --------------------------------------------------------------------------- #


def test_pjn_camaras_free() -> None:
    """PJN camaras listing is captcha-free: validates the relocated source.

    ``camaras`` returns the chamber/jurisdiction ids accepted by ``--camara`` and
    does **not** launch the browser or solve a captcha. This is the free op the
    orchestrator validates live in this step.
    """
    env = dispatch("pjn-expedientes", "camaras")
    env = assert_ok_envelope(env)
    items = env.get("items")
    assert isinstance(items, list) and items, (
        f"PJN camaras returned no jurisdictions: {env!r}"
    )


def test_pjn_expediente_browser(requires_capsolver: str) -> None:
    """PJN expediente lookup drives BotBrowser + solves the image captcha.

    Launches the relocated BotBrowser, fills the public expediente search tab,
    and solves the PJN image challenge via the Capsolver ``ImageToTextTask``
    backend (spends credits). Gated by ``requires_capsolver`` and uses
    ``--retries 3`` because the image solve is probabilistic. A specific
    ``--numero``/``--anio`` that legitimately has no docket yields ``ok:true``
    with no result rows (``no_results``), which is acceptable; a browser-launch
    or captcha failure is a relocation regression.
    """
    env = dispatch(
        "pjn-expedientes",
        "expediente",
        camara="10",
        numero="12345",
        anio="2024",
        retries=PJN_RETRIES,
    )
    assert_ok_envelope(env)
