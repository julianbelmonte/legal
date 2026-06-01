"""Portable search-state cache for the legal CLI."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


JsonDict = dict[str, Any]

CACHE_ENV_VAR = "LEGAL_CACHE_DIR"
DEFAULT_APP_NAME = "webcam-legal"
DEFAULT_SEARCH_TTL = timedelta(hours=6)
SEARCH_STATE_DIR = "search-state"
SEARCH_STATE_VERSION = 1

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    return _coerce_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(raw)
    return _coerce_utc(parsed)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
            if getattr(value, item.name) is not None
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _stable_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def get_cache_dir(app_name: str = DEFAULT_APP_NAME) -> Path:
    """Return the legal CLI cache directory without creating it."""
    override = os.environ.get(CACHE_ENV_VAR)
    if override:
        return Path(os.path.expandvars(override)).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / app_name
        return Path.home() / "AppData" / "Local" / app_name

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / app_name

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(os.path.expandvars(xdg_cache)).expanduser() / app_name

    return Path.home() / ".cache" / app_name


def ensure_cache_dir(app_name: str = DEFAULT_APP_NAME) -> Path:
    """Create and return the legal CLI cache directory."""
    path = get_cache_dir(app_name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(app_name: str = DEFAULT_APP_NAME) -> Path:
    """Backward-compatible alias for get_cache_dir."""
    return get_cache_dir(app_name)


def query_hash(query: Mapping[str, Any]) -> str:
    """Return a stable hash for a normalized search query."""
    return hashlib.sha256(_stable_json(query).encode("utf-8")).hexdigest()


def _safe_filename(value: str) -> str:
    safe = _SAFE_FILENAME.sub("-", value).strip(".-")
    if safe:
        return safe[:180]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_search_id(source: str, query: Mapping[str, Any]) -> str:
    """Create a path-safe search id with source and query hash context."""
    source_part = _safe_filename(source.lower()) or "source"
    return f"{source_part}-{query_hash(query)[:12]}-{uuid.uuid4().hex[:12]}"


def get_search_state_dir(*, base_dir: Path | None = None) -> Path:
    """Return the directory where search-state records are stored."""
    return (base_dir or get_cache_dir()) / SEARCH_STATE_DIR


def get_search_state_path(search_id: str, *, base_dir: Path | None = None) -> Path:
    """Return the JSON record path for a search id."""
    return get_search_state_dir(base_dir=base_dir) / f"{_safe_filename(search_id)}.json"


def _cookie_from_mapping(cookie: Mapping[str, Any]) -> JsonDict:
    allowed = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "secure",
        "http_only",
        "httponly",
        "same_site",
        "samesite",
    }
    normalized = {str(key): _jsonable(value) for key, value in cookie.items() if str(key) in allowed}
    if "httponly" in normalized and "http_only" not in normalized:
        normalized["http_only"] = normalized.pop("httponly")
    if "samesite" in normalized and "same_site" not in normalized:
        normalized["same_site"] = normalized.pop("samesite")
    return normalized


def _cookie_from_object(cookie: Any) -> JsonDict:
    normalized: JsonDict = {}
    for source_name, target_name in (
        ("name", "name"),
        ("value", "value"),
        ("domain", "domain"),
        ("path", "path"),
        ("expires", "expires"),
        ("secure", "secure"),
    ):
        if hasattr(cookie, source_name):
            normalized[target_name] = _jsonable(getattr(cookie, source_name))
    if hasattr(cookie, "has_nonstandard_attr"):
        normalized["http_only"] = bool(cookie.has_nonstandard_attr("HttpOnly"))
    return normalized


def _normalize_cookies(cookies: Any) -> list[JsonDict]:
    if cookies is None:
        return []
    if isinstance(cookies, Mapping):
        return [{"name": str(name), "value": str(value)} for name, value in sorted(cookies.items())]
    if hasattr(cookies, "jar"):
        cookies = cookies.jar

    normalized: list[JsonDict] = []
    for cookie in cookies:
        item = _cookie_from_mapping(cookie) if isinstance(cookie, Mapping) else _cookie_from_object(cookie)
        if "name" in item and "value" in item:
            normalized.append(item)
    return normalized


@dataclass(frozen=True)
class SearchCacheRecord:
    """State needed to continue a source search without browser automation."""

    search_id: str
    source: str
    query_hash: str
    created_at: str
    expiry: str
    cookies: list[JsonDict] = field(default_factory=list)
    hidden_fields: JsonDict = field(default_factory=dict)
    cursor_payload: JsonDict = field(default_factory=dict)
    raw_provenance: JsonDict = field(default_factory=dict)
    version: int = SEARCH_STATE_VERSION

    @classmethod
    def build(
        cls,
        *,
        source: str,
        query: Mapping[str, Any],
        ttl: timedelta = DEFAULT_SEARCH_TTL,
        search_id: str | None = None,
        cookies: Any = None,
        store_cookies: bool = False,
        hidden_fields: Mapping[str, Any] | None = None,
        cursor_payload: Mapping[str, Any] | None = None,
        raw_provenance: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> "SearchCacheRecord":
        created = _coerce_utc(now)
        return cls(
            search_id=search_id or new_search_id(source, query),
            source=source,
            query_hash=query_hash(query),
            created_at=_utc_iso(created),
            expiry=_utc_iso(created + ttl),
            cookies=_normalize_cookies(cookies) if store_cookies else [],
            hidden_fields=_jsonable(hidden_fields or {}),
            cursor_payload=_jsonable(cursor_payload or {}),
            raw_provenance=_jsonable(raw_provenance or {}),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SearchCacheRecord":
        expiry = payload.get("expiry") or payload.get("expires_at")
        if not isinstance(expiry, str):
            raise ValueError("cache record is missing expiry")
        return cls(
            version=int(payload.get("version", SEARCH_STATE_VERSION)),
            search_id=str(payload["search_id"]),
            source=str(payload["source"]),
            query_hash=str(payload["query_hash"]),
            created_at=str(payload["created_at"]),
            expiry=expiry,
            cookies=list(payload.get("cookies") or []),
            hidden_fields=dict(payload.get("hidden_fields") or {}),
            cursor_payload=dict(payload.get("cursor_payload") or {}),
            raw_provenance=dict(payload.get("raw_provenance") or {}),
        )

    def to_dict(self) -> JsonDict:
        return {
            "version": self.version,
            "search_id": self.search_id,
            "source": self.source,
            "query_hash": self.query_hash,
            "created_at": self.created_at,
            "expiry": self.expiry,
            "cookies": self.cookies,
            "hidden_fields": self.hidden_fields,
            "cursor_payload": self.cursor_payload,
            "raw_provenance": self.raw_provenance,
        }

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return _parse_utc(self.expiry) <= _coerce_utc(now)


def write_search_state(record: SearchCacheRecord, *, base_dir: Path | None = None) -> Path:
    """Atomically write a search-state JSON record and return its path."""
    path = get_search_state_path(record.search_id, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(record.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return path


def save_search_state(
    *,
    source: str,
    query: Mapping[str, Any],
    ttl: timedelta = DEFAULT_SEARCH_TTL,
    search_id: str | None = None,
    cookies: Any = None,
    store_cookies: bool = False,
    hidden_fields: Mapping[str, Any] | None = None,
    cursor_payload: Mapping[str, Any] | None = None,
    raw_provenance: Mapping[str, Any] | None = None,
    base_dir: Path | None = None,
    now: datetime | None = None,
) -> SearchCacheRecord:
    """Build and persist a search-state record.

    Cookies are persisted only when ``store_cookies`` is true, so source
    adapters have to make an explicit safety decision before writing them.
    """
    record = SearchCacheRecord.build(
        source=source,
        query=query,
        ttl=ttl,
        search_id=search_id,
        cookies=cookies,
        store_cookies=store_cookies,
        hidden_fields=hidden_fields,
        cursor_payload=cursor_payload,
        raw_provenance=raw_provenance,
        now=now,
    )
    write_search_state(record, base_dir=base_dir)
    return record


def load_search_state(
    search_id: str,
    *,
    base_dir: Path | None = None,
    now: datetime | None = None,
    delete_expired: bool = True,
) -> SearchCacheRecord | None:
    """Load a non-expired search-state record by id."""
    path = get_search_state_path(search_id, base_dir=base_dir)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("cache record must be a JSON object")
    record = SearchCacheRecord.from_dict(payload)
    if record.is_expired(now=now):
        if delete_expired:
            path.unlink(missing_ok=True)
        return None
    return record


def delete_search_state(search_id: str, *, base_dir: Path | None = None) -> bool:
    """Delete a cached search-state record."""
    path = get_search_state_path(search_id, base_dir=base_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def cleanup_expired_search_states(
    *,
    base_dir: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Remove expired search-state records and return the count deleted."""
    state_dir = get_search_state_dir(base_dir=base_dir)
    if not state_dir.exists():
        return 0

    removed = 0
    for path in state_dir.glob("*.json"):
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            continue
        if SearchCacheRecord.from_dict(payload).is_expired(now=now):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


__all__ = [
    "CACHE_ENV_VAR",
    "DEFAULT_APP_NAME",
    "DEFAULT_SEARCH_TTL",
    "SEARCH_STATE_DIR",
    "SEARCH_STATE_VERSION",
    "SearchCacheRecord",
    "cache_dir",
    "cleanup_expired_search_states",
    "delete_search_state",
    "ensure_cache_dir",
    "get_cache_dir",
    "get_search_state_dir",
    "get_search_state_path",
    "load_search_state",
    "new_search_id",
    "query_hash",
    "save_search_state",
    "write_search_state",
]
