#!/usr/bin/env python3
"""Harvest a corpus of real CSJN PDFs for PDF->text extraction stress tests.

This is a *runnable script*, not a pytest test. It reuses the pipeline's shared
dispatch seam (``legal.dispatch.run_operation``) to drive CSJN's browser-backed
``fallos`` search and ``download`` operations -- it never reimplements source
access. CSJN uses BotBrowser under a hidden Xvfb display with native reCAPTCHA
Enterprise scoring (no Capsolver credits, no secrets), but each attempt is
probabilistic (WAF 502s/403s, score rejections), so this script runs its own
generous retry/budget loop across several narrowed queries and many ids.

The corpus is cached under ``LEGAL_CSJN_CORPUS`` (default ``.work/csjn_corpus``),
which lives in the gitignored ``.work/`` tree -- nothing here is committed.

Usage:
    LEGAL_LIVE=1 uv run python tests/live/harvest_csjn_corpus.py --target 25
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from legal.dispatch import run_operation

# Distinct narrowed queries. Each stays well under CSJN's 5000-row cap by
# combining a free-text term with a bounded decision-date window, and the set
# spans different subject matters / years so the harvested documents differ.
QUERIES: list[dict[str, Any]] = [
    {"texto": "arbitrariedad", "fecha_desde": "2023-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "amparo ambiental", "fecha_desde": "2022-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "prescripcion", "fecha_desde": "2021-01-01", "fecha_hasta": "2022-12-31"},
    {"texto": "jubilacion movilidad", "fecha_desde": "2021-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "responsabilidad del estado", "fecha_desde": "2020-01-01", "fecha_hasta": "2022-12-31"},
    {"texto": "impuesto", "fecha_desde": "2022-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "despido", "fecha_desde": "2021-01-01", "fecha_hasta": "2022-12-31"},
    {"texto": "habeas corpus", "fecha_desde": "2019-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "competencia federal", "fecha_desde": "2022-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "honorarios", "fecha_desde": "2021-01-01", "fecha_hasta": "2022-12-31"},
    {"texto": "extradicion", "fecha_desde": "2018-01-01", "fecha_hasta": "2023-12-31"},
    {"texto": "consumidor", "fecha_desde": "2020-01-01", "fecha_hasta": "2023-12-31"},
]


def _resp_to_dict(resp: Any) -> dict[str, Any]:
    to_dict = getattr(resp, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(resp, dict):
        return resp
    return {}


def _doc_id_from_item(item: dict[str, Any]) -> str | None:
    fields = item.get("source_fields") or {}
    doc_id = fields.get("doc_id") or item.get("id")
    if doc_id is None:
        return None
    text = str(doc_id).strip()
    # Item ids may be prefixed (e.g. "csjn:..."); keep only a bare numeric id.
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    return text if text.isdigit() else None


def collect_ids(
    *,
    per_query_limit: int,
    search_retries: int,
    deadline: float,
    enough: int | None = None,
) -> list[str]:
    """Run the narrowed queries and collect unique numeric idDocumento values.

    Iterates across *all* queries until either the discovery deadline is hit or
    ``enough`` candidate ids have been collected (so a productive early query
    can short-circuit discovery and let downloads start). Each query bounds its
    own cost via a low ``search_retries`` so no single query can monopolise the
    discovery window.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for q in QUERIES:
        if enough is not None and len(seen) >= enough:
            print(f"  [search] collected {len(seen)} ids (>= {enough}); stopping discovery early")
            break
        if time.monotonic() >= deadline:
            print("  [search] wall-clock budget exhausted; stopping discovery")
            break
        params = dict(q)
        params["limit"] = per_query_limit
        params["retries"] = search_retries
        label = q.get("texto", "?")
        try:
            resp = run_operation("csjn", "fallos", params)
        except Exception as exc:  # noqa: BLE001 - probabilistic source; keep going
            print(f"  [search] query {label!r} raised {type(exc).__name__}: {exc}")
            continue
        data = _resp_to_dict(resp)
        if not data.get("ok"):
            err = data.get("error") or {}
            print(f"  [search] query {label!r} not ok: {err.get('code')} {err.get('message')}")
            continue
        items = data.get("items") or []
        new = 0
        for item in items:
            doc_id = _doc_id_from_item(item)
            if doc_id and doc_id not in seen_set:
                seen_set.add(doc_id)
                seen.append(doc_id)
                new += 1
        print(f"  [search] query {label!r}: {len(items)} items, +{new} new ids (total {len(seen)})")
    return seen


def fetch_pdf_bytes(doc_id: str) -> bytes | None:
    """Fetch raw PDF bytes for a doc id via the CSJN download path.

    Writes via the handler's own ``save_pdf`` to a temp path, then reads bytes
    back; this reuses ``_fetch_pdf_via_context`` exactly as the pipeline does.
    """
    tmp = Path(f".work/.csjn_tmp_{doc_id}.pdf")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    try:
        resp = run_operation(
            "csjn",
            "download",
            {"id": doc_id, "save_pdf": str(tmp)},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"      download raised {type(exc).__name__}: {exc}")
        return None
    data = _resp_to_dict(resp)
    if not data.get("ok"):
        err = data.get("error") or {}
        print(f"      not ok: {err.get('code')} {err.get('message')}")
        if tmp.exists():
            tmp.unlink()
        return None
    if tmp.exists():
        body = tmp.read_bytes()
        tmp.unlink()
        return body
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Harvest real CSJN PDFs into a cached corpus")
    parser.add_argument("--target", type=int, default=25, help="number of distinct PDFs to cache")
    parser.add_argument("--per-query-limit", type=int, default=50, help="search limit per query")
    parser.add_argument("--search-retries", type=int, default=2, help="retries per fallos search")
    parser.add_argument("--download-retries", type=int, default=5, help="retries per id download")
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=1800.0,
        help="hard overall wall-clock budget; always terminates",
    )
    args = parser.parse_args()

    corpus_dir = Path(os.environ.get("LEGAL_CSJN_CORPUS", ".work/csjn_corpus"))
    corpus_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    deadline = start + args.budget_seconds

    existing = {p.stem for p in corpus_dir.glob("*.pdf")}
    print(f"corpus dir: {corpus_dir} (already cached: {len(existing)})")
    print(f"target: {args.target}, budget: {args.budget_seconds:.0f}s")

    attempts = 0
    skips_existing = 0
    skips_nonpdf = 0
    failures = 0

    cached = len(existing)
    # Reserve ~60% of the budget for discovery so several narrowed queries get
    # attempted (each is cheap with low search-retries), but let discovery
    # return early once we have plenty of candidate ids so downloads can start.
    discovery_deadline = min(deadline, start + args.budget_seconds * 0.6)
    enough = max(args.target * 2, args.target + 1)
    print("discovering idDocumento values via fallos searches...")
    ids = collect_ids(
        per_query_limit=args.per_query_limit,
        search_retries=args.search_retries,
        deadline=discovery_deadline,
        enough=enough,
    )
    # Drop ids whose PDF is already cached.
    ids = [d for d in ids if d not in existing]
    print(f"discovered {len(ids)} candidate ids to download")

    for doc_id in ids:
        if cached >= args.target:
            print(f"reached target {args.target}; stopping")
            break
        if time.monotonic() >= deadline:
            print("wall-clock budget exhausted; stopping downloads")
            break
        out = corpus_dir / f"{doc_id}.pdf"
        if out.exists():
            skips_existing += 1
            continue
        got = False
        for attempt in range(1, args.download_retries + 1):
            if time.monotonic() >= deadline:
                break
            attempts += 1
            print(f"  download id={doc_id} attempt {attempt}/{args.download_retries}")
            body = fetch_pdf_bytes(doc_id)
            if body and body[:4] == b"%PDF":
                out.write_bytes(body)
                cached += 1
                got = True
                print(f"    cached {out.name} ({len(body)} bytes); total cached {cached}")
                break
            if body is not None:
                skips_nonpdf += 1
                print(f"    non-PDF/empty body ({len(body)} bytes); skipping content")
        if not got:
            failures += 1

    print("\n=== harvest summary ===")
    print(f"corpus dir        : {corpus_dir}")
    print(f"cached PDFs (total): {cached}")
    print(f"download attempts : {attempts}")
    print(f"skips (existing)  : {skips_existing}")
    print(f"skips (non-PDF)   : {skips_nonpdf}")
    print(f"ids that failed   : {failures}")
    print(f"elapsed           : {time.monotonic() - start:.0f}s")

    return 0 if cached >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
