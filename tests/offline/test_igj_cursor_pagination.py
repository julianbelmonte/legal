"""Regression tests for IGJ cursor pagination.

Bug (found via stress test): `igj list`/`scrape-official-page` read the query
context (`year`, `url`) from the cursor's top level, but pagination cursors
store it nested under ``raw``. So following a returned ``next_cursor`` always
failed with "--year is required" / "--url is required". These tests lock the
fix: the handlers must read the context from the cursor's ``raw`` block.
"""
from __future__ import annotations

import argparse
import contextlib

import pytest

from legal.errors import LegalCliError
from legal.sources import igj


def test_cursor_context_reads_nested_raw():
    payload = {
        "limit": 5, "offset": 5, "operation": "list",
        "raw": {"url": "https://x/y", "year": 2024},
        "source": "igj", "version": 1,
    }
    ctx = igj._cursor_context(payload)
    assert ctx.get("year") == 2024
    assert ctx.get("url") == "https://x/y"


def test_cursor_context_empty_and_missing_raw():
    assert igj._cursor_context({}) == {}
    assert igj._cursor_context({"raw": None}) == {}
    assert igj._cursor_context({"raw": "notamap"}) == {}


def test_handle_list_requires_year_without_cursor():
    args = argparse.Namespace(cursor=None, year=None, limit=None)
    with pytest.raises(LegalCliError) as exc:
        igj.handle_list(args)
    assert exc.value.code == "usage_error"


def test_handle_list_accepts_year_from_cursor(monkeypatch):
    # cursor carries the year under raw -> handler must NOT demand --year
    decoded = {
        "limit": 5, "offset": 5, "operation": "list",
        "raw": {"url": "https://www.argentina.gob.ar/igj/2024", "year": 2024},
        "source": "igj", "version": 1,
    }
    monkeypatch.setattr(igj, "_decode_cursor", lambda *a, **k: decoded)

    captured = {}

    def fake_fetch(page_url, *, client, year):
        captured["page_url"] = page_url
        captured["year"] = year
        return igj.OfficialPage(url=page_url, items=[], headers={}, year=year)

    monkeypatch.setattr(igj, "fetch_official_page", fake_fetch)
    monkeypatch.setattr(igj, "_make_client", lambda: contextlib.nullcontext(object()))

    args = argparse.Namespace(cursor="opaque", year=None, limit=5)
    resp = igj.handle_list(args)
    assert resp.ok is True
    assert captured["year"] == 2024  # year resolved from cursor.raw, not args
