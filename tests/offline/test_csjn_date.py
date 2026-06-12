"""Offline tests for CSJN search date coercion.

CSJN's fallos form only accepts Argentine ``DD/MM/YYYY`` dates and silently
refuses to submit on a malformed value (the page then never navigates, which is
easily misread as a captcha rejection). ``_csjn_date`` normalizes the ISO dates
callers naturally pass into that required form. These tests pin that contract
without touching the network or the browser.
"""

from __future__ import annotations

import pytest

from legal.sources.csjn import _csjn_date


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2020-01-01", "01/01/2020"),   # ISO -> DD/MM/YYYY (the bug this fixes)
        ("2020-12-31", "31/12/2020"),
        ("01/01/2020", "01/01/2020"),   # already DD/MM/YYYY -> unchanged
        ("31/12/2020", "31/12/2020"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_csjn_date_normalizes_to_ddmmyyyy(value, expected) -> None:
    assert _csjn_date(value) == expected


def test_csjn_date_never_emits_iso() -> None:
    """An ISO date must never reach the form field; that is what broke submits."""
    assert _csjn_date("2024-06-15") == "15/06/2024"
    assert "-" not in _csjn_date("2024-06-15")
