"""Unit tests for the MCP JSON serialization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

from legal.models import LegalError, LegalItem, LegalResponse
from server.serialization import (
    SerializationError,
    error_envelope,
    to_jsonable,
)


def test_legal_response_preserves_to_dict() -> None:
    response = LegalResponse.search(
        source="x",
        operation="search",
        query={"text": "ley"},
        items=[LegalItem(id="1", title="t")],
    )
    result = to_jsonable(response)
    assert result == response.to_dict()
    assert result["ok"] is True
    assert result["items"][0]["id"] == "1"


def test_error_response_round_trip() -> None:
    response = LegalResponse.error_response(
        source="x",
        operation="search",
        error=LegalError(code="boom", message="nope"),
    )
    result = to_jsonable(response)
    assert result["ok"] is False
    assert result["error"]["code"] == "boom"


def test_plain_mapping_passthrough() -> None:
    assert to_jsonable({"a": 1, "b": [1, 2, 3]}) == {"a": 1, "b": [1, 2, 3]}


def test_non_string_keys_coerced() -> None:
    assert to_jsonable({1: "a"}) == {"1": "a"}


def test_dataclass_without_to_dict() -> None:
    @dataclass
    class Plain:
        name: str
        skip: int | None = None

    assert to_jsonable(Plain(name="ok")) == {"name": "ok"}


def test_scalars_and_dates() -> None:
    assert to_jsonable(date(2024, 1, 2)) == "2024-01-02"
    assert to_jsonable(datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == (
        "2024-01-02T03:04:05Z"
    )
    assert to_jsonable(Path("/tmp/x")) == "/tmp/x"


def test_enum_value() -> None:
    class Color(Enum):
        RED = "red"

    assert to_jsonable(Color.RED) == "red"


def test_set_serialized_as_list() -> None:
    assert sorted(to_jsonable({3, 1, 2})) == [1, 2, 3]


def test_bytes_rejected() -> None:
    with pytest.raises(SerializationError):
        to_jsonable(b"binary")


def test_non_serializable_rejected() -> None:
    with pytest.raises(SerializationError):
        to_jsonable(object())


def test_error_envelope_shape() -> None:
    env = error_envelope(source="x", operation="search", message="bad")
    assert env["ok"] is False
    assert env["source"] == "x"
    assert env["error"]["code"] == "serialization_error"
    assert env["error"]["message"] == "bad"
