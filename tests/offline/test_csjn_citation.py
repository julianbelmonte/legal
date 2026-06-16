"""Offline tests for CSJN Fallos-citation (tomo:pagina) sumarios search.

These never launch a browser: they drive ``_fill_sumarios_form`` with a fake
page that records the field writes, proving a cite like 315:2616 is routed into
the sumarios form's dedicated ``filter.tomo`` / ``filter.pagina`` inputs.
"""

from __future__ import annotations

import argparse

from legal.sources import csjn


class _FakeEl:
    def click(self) -> None:  # pragma: no cover - trivial
        pass


class _FakePage:
    """Minimal page double recording filter-field writes and typed text."""

    def __init__(self) -> None:
        self.set_fields: dict[str, str] = {}
        self.typed: list[tuple[str, str]] = []

    def evaluate(self, script: str, arg=None):
        # The fullText selector-detection call passes no arg; return a selector.
        if arg is None:
            return '[name="filter.fullText"]'
        # _set_filter_field passes [name, value].
        name, value = arg
        self.set_fields[name] = value

    def query_selector(self, _selector: str) -> _FakeEl:
        return _FakeEl()

    def type(self, selector: str, text: str, delay: int = 0) -> None:
        self.typed.append((selector, text))


def _sumarios_args(**kwargs) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    csjn.add_sumarios_arguments(parser)
    defaults = {"texto": "", "tomo": None, "pagina": None}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_citation_fills_tomo_and_pagina_filters() -> None:
    page = _FakePage()
    csjn._fill_sumarios_form(page, _sumarios_args(tomo="315", pagina="2616"))
    assert page.set_fields.get("filter.tomo") == "315"
    assert page.set_fields.get("filter.pagina") == "2616"


def test_no_citation_leaves_filters_unset() -> None:
    page = _FakePage()
    csjn._fill_sumarios_form(page, _sumarios_args(texto="amparo"))
    assert "filter.tomo" not in page.set_fields
    assert "filter.pagina" not in page.set_fields
    assert page.typed and page.typed[0][1] == "amparo"


def test_citation_echoed_in_query() -> None:
    query = csjn._sumarios_query_from_args(_sumarios_args(tomo="327", pagina="3753"))
    assert query["tomo"] == "327"
    assert query["pagina"] == "3753"
