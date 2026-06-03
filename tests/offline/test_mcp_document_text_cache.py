"""Unit tests for the MCP document text TTL cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.document_text.cache import DocumentTextCache, DocumentTextRecord


def test_put_get_round_trip(tmp_path: Path) -> None:
    cache = DocumentTextCache(base_dir=tmp_path)
    record = cache.put(
        source="csjn",
        document_ref={"id": "42"},
        text="extracted body",
        metadata={"court": "CSJN"},
        title="Fallo",
        date="2024-01-01",
        url="https://example/doc",
        provenance={"operation": "documento"},
    )
    loaded = cache.get(record.cache_id)
    assert loaded is not None
    assert loaded.text == "extracted body"
    assert loaded.source == "csjn"
    assert loaded.document_ref == {"id": "42"}
    assert loaded.title == "Fallo"
    assert loaded.metadata == {"court": "CSJN"}
    assert loaded.provenance == {"operation": "documento"}
    assert loaded.query_hash  # derived from document_ref when not provided


def test_get_missing_returns_none(tmp_path: Path) -> None:
    cache = DocumentTextCache(base_dir=tmp_path)
    assert cache.get("does-not-exist") is None


def test_expired_record_evicted(tmp_path: Path) -> None:
    cache = DocumentTextCache(base_dir=tmp_path, ttl=timedelta(seconds=1))
    record = cache.put(source="saij", document_ref={"id": "1"}, text="x")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert cache.get(record.cache_id, now=later) is None
    # delete_expired (default) removed the file.
    assert cache.get(record.cache_id) is None


def test_record_never_stores_pdf_bytes(tmp_path: Path) -> None:
    cache = DocumentTextCache(base_dir=tmp_path)
    record = cache.put(source="ptn", document_ref={"id": "9"}, text="text only")
    payload = record.to_dict()
    assert "pdf" not in payload
    assert "bytes" not in payload
    assert set(payload) >= {"cache_id", "source", "text", "expiry", "created_at"}


def test_from_dict_round_trip() -> None:
    record = DocumentTextRecord.build(
        source="infoleg",
        document_ref={"id": "7"},
        text="norm text",
        ttl=timedelta(hours=2),
    )
    assert DocumentTextRecord.from_dict(record.to_dict()) == record
