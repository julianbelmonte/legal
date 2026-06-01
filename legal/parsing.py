"""Parsing helpers shared by legal source adapters.

The legal sources mix JSON APIs with older HTML pages and fragments. These
helpers intentionally stay stdlib-only so source adapters remain portable on
Linux and Windows.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlparse


JsonDict = dict[str, Any]

_WHITESPACE = re.compile(r"[\s\u00a0]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_CHARS = re.compile(r"[\u200b-\u200f\ufeff]")
_NUMERIC_DATE_RE = re.compile(r"(?<!\d)(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{2,4})(?!\d)")
_ISO_DATE_RE = re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)")
_TEXTUAL_DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+(?:de\s+)?(?P<month>[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ.]+)"
    r"\s+(?:de\s+)?(?P<year>\d{2,4})(?!\d)"
)

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SKIP_TEXT_TAGS = {"script", "style", "template"}
_BLOCK_TEXT_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "caption",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_UNSAFE_SCHEMES = {"about", "blob", "chrome", "data", "file", "javascript", "vbscript"}

_MONTHS = {
    "ene": 1,
    "enero": 1,
    "feb": 2,
    "febrero": 2,
    "mar": 3,
    "marzo": 3,
    "abr": 4,
    "abril": 4,
    "may": 5,
    "mayo": 5,
    "jun": 6,
    "junio": 6,
    "jul": 7,
    "julio": 7,
    "ago": 8,
    "agosto": 8,
    "sep": 9,
    "sept": 9,
    "set": 9,
    "septiembre": 9,
    "setiembre": 9,
    "oct": 10,
    "octubre": 10,
    "nov": 11,
    "noviembre": 11,
    "dic": 12,
    "diciembre": 12,
}

_CONTENT_TYPE_KINDS = {
    "application/pdf": "pdf",
    "application/msword": "document",
    "application/rtf": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "text/csv": "spreadsheet",
    "application/json": "data",
    "application/xml": "data",
    "text/xml": "data",
    "application/zip": "archive",
}
_EXTENSION_KINDS = {
    ".pdf": "pdf",
    ".doc": "document",
    ".docx": "document",
    ".odt": "document",
    ".rtf": "document",
    ".xls": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".ods": "spreadsheet",
    ".csv": "spreadsheet",
    ".json": "data",
    ".xml": "data",
    ".zip": "archive",
    ".rar": "archive",
    ".7z": "archive",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
}


@dataclass(slots=True)
class HtmlNode:
    """Small HTML tree node used by stdlib-only extraction helpers."""

    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[HtmlNode | str] = field(default_factory=list)
    parent: HtmlNode | None = field(default=None, repr=False, compare=False)

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name.lower(), default)

    def iter(self, tag: str | None = None) -> Iterator[HtmlNode]:
        normalized = tag.lower() if tag else None
        if normalized is None or self.tag == normalized:
            yield self
        for child in self.children:
            if isinstance(child, HtmlNode):
                yield from child.iter(normalized)

    def find(self, tag: str) -> HtmlNode | None:
        return next(self.iter(tag), None)

    def find_all(self, tag: str) -> list[HtmlNode]:
        return list(self.iter(tag))

    def text(self) -> str | None:
        return clean_text(" ".join(_node_text_parts(self)))


class _HtmlTreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("[document]")
        self._stack = [self.root]

    @property
    def current(self) -> HtmlNode:
        return self._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._build_node(tag, attrs)
        self.current.children.append(node)
        node.parent = self.current
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._build_node(tag, attrs)
        self.current.children.append(node)
        node.parent = self.current

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.current.children.append(data)

    @staticmethod
    def _build_node(tag: str, attrs: list[tuple[str, str | None]]) -> HtmlNode:
        return HtmlNode(
            tag=tag.lower(),
            attrs={name.lower(): (_clean_attr_value(value) if value is not None else None) for name, value in attrs},
        )


def _clean_attr_value(value: Any) -> str:
    return _WHITESPACE.sub(" ", unescape(str(value))).strip()


def _node_text_parts(node: HtmlNode) -> list[str]:
    if node.tag in _SKIP_TEXT_TAGS:
        return []

    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
            continue
        if child.tag in _BLOCK_TEXT_TAGS:
            parts.append(" ")
        parts.extend(_node_text_parts(child))
        if child.tag in _BLOCK_TEXT_TAGS:
            parts.append(" ")
    return parts


def _coerce_html_node(value: str | HtmlNode | None) -> HtmlNode:
    if isinstance(value, HtmlNode):
        return value
    return parse_html(value)


def _direct_children(node: HtmlNode, tags: set[str]) -> list[HtmlNode]:
    return [child for child in node.children if isinstance(child, HtmlNode) and child.tag in tags]


def _nearest_ancestor(node: HtmlNode, tag: str) -> HtmlNode | None:
    current = node.parent
    while current is not None:
        if current.tag == tag:
            return current
        current = current.parent
    return None


def clean_text(value: str | None) -> str | None:
    """Normalize HTML entities and whitespace, returning ``None`` for blanks."""
    if value is None:
        return None
    cleaned = unescape(str(value))
    cleaned = _ZERO_WIDTH_CHARS.sub("", cleaned)
    cleaned = _CONTROL_CHARS.sub(" ", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or None


def parse_html(value: str | None) -> HtmlNode:
    """Parse an HTML document or fragment into a lightweight tree."""
    parser = _HtmlTreeBuilder()
    if value:
        parser.feed(value)
        parser.close()
    return parser.root


def text_content(value: str | HtmlNode | None) -> str | None:
    """Return visible text from an HTML fragment or parsed node."""
    return _coerce_html_node(value).text()


def clean_snippet(value: str | HtmlNode | None, *, max_length: int | None = None) -> str | None:
    """Strip HTML/control characters from a search snippet without executing markup."""
    snippet = text_content(value) if isinstance(value, HtmlNode) or _looks_like_html(value) else clean_text(value)
    if snippet is None or max_length is None or len(snippet) <= max_length:
        return snippet
    if max_length < 1:
        raise ValueError("max_length must be greater than zero")

    prefix = snippet[:max_length].rstrip()
    boundary = prefix.rfind(" ")
    if boundary >= max_length // 2:
        prefix = prefix[:boundary].rstrip()
    return f"{prefix}..."


def _looks_like_html(value: str | HtmlNode | None) -> bool:
    return isinstance(value, HtmlNode) or (isinstance(value, str) and "<" in value and ">" in value)


def absolute_url(base_url: str | None, href: str | None, *, allowed_schemes: Sequence[str] = ("http", "https")) -> str | None:
    """Resolve ``href`` against ``base_url`` and reject unsafe/non-web schemes."""
    raw = _clean_attr_value(href) if href is not None else None
    if not raw or raw.startswith("#"):
        return None

    allowed = {scheme.lower() for scheme in allowed_schemes}
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme.lower() in allowed:
            return raw
        return None

    if raw.startswith("//"):
        base_scheme = urlparse(base_url or "").scheme or "https"
        resolved = f"{base_scheme}:{raw}"
    elif base_url:
        resolved = urljoin(base_url, raw)
    else:
        return None

    resolved_scheme = urlparse(resolved).scheme.lower()
    if resolved_scheme not in allowed:
        return None
    return resolved


def parse_argentine_date(value: str | date | datetime | None) -> date | None:
    """Parse common Argentine date formats and return a ``date`` object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = clean_text(value)
    if not text:
        return None

    for match in _ISO_DATE_RE.finditer(text):
        parsed = _build_date(match.group("day"), match.group("month"), match.group("year"))
        if parsed:
            return parsed

    for match in _NUMERIC_DATE_RE.finditer(text):
        parsed = _build_date(match.group("day"), match.group("month"), match.group("year"))
        if parsed:
            return parsed

    for match in _TEXTUAL_DATE_RE.finditer(text):
        month = _month_number(match.group("month"))
        if month is None:
            continue
        parsed = _build_date(match.group("day"), str(month), match.group("year"))
        if parsed:
            return parsed
    return None


def normalize_date(value: str | date | datetime | None) -> str | None:
    """Parse a date-like value and return an ISO ``YYYY-MM-DD`` string."""
    parsed = parse_argentine_date(value)
    return parsed.isoformat() if parsed else None


def _build_date(day: str, month: str, year: str) -> date | None:
    try:
        return date(_expand_year(int(year)), int(month), int(day))
    except ValueError:
        return None


def _expand_year(year: int) -> int:
    if year < 100:
        return 2000 + year if year < 70 else 1900 + year
    return year


def _month_number(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.lower().strip(".")
    return _MONTHS.get(normalized)


def extract_tables(value: str | HtmlNode | None) -> list[JsonDict]:
    """Extract HTML tables as ``{"headers": [...], "rows": [...]}`` records."""
    root = _coerce_html_node(value)
    tables: list[JsonDict] = []
    for table in root.iter("table"):
        rows = _table_rows(table)
        headers, data_rows = _table_headers_and_rows(rows)
        tables.append(
            {
                "headers": headers,
                "rows": [_map_table_row(row, headers) for row in data_rows if _row_cell_text(row)],
            }
        )
    return tables


def extract_table_rows(value: str | HtmlNode | None, *, index: int = 0) -> list[JsonDict]:
    """Return normalized row dictionaries from one table."""
    tables = extract_tables(value)
    if index < 0:
        raise ValueError("index must be greater than or equal to zero")
    if index >= len(tables):
        return []
    return list(tables[index]["rows"])


def _table_rows(table: HtmlNode) -> list[HtmlNode]:
    rows: list[HtmlNode] = []
    for child in table.children:
        if not isinstance(child, HtmlNode):
            continue
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            rows.extend(_direct_children(child, {"tr"}))
    if rows:
        return rows
    return [row for row in table.iter("tr") if _nearest_ancestor(row, "table") is table]


def _table_headers_and_rows(rows: list[HtmlNode]) -> tuple[list[str], list[HtmlNode]]:
    if not rows:
        return [], []

    thead_rows = [row for row in rows if _nearest_ancestor(row, "thead") is not None]
    header_row = next((row for row in thead_rows if _row_cell_text(row)), None)
    if header_row is None:
        header_row = next((row for row in rows if _direct_children(row, {"th"})), None)

    if header_row is None:
        return [], rows

    headers = _row_cell_text(header_row)
    if header_row in thead_rows:
        return headers, [row for row in rows if _nearest_ancestor(row, "thead") is None]
    return headers, [row for row in rows if row is not header_row]


def _row_cell_text(row: HtmlNode) -> list[str]:
    return [cell.text() or "" for cell in _direct_children(row, {"td", "th"})]


def _map_table_row(row: HtmlNode, headers: list[str]) -> JsonDict:
    cells = _row_cell_text(row)
    mapped: JsonDict = {}
    for index, value in enumerate(cells):
        header = headers[index] if index < len(headers) and headers[index] else f"column_{index + 1}"
        mapped[_unique_key(mapped, header)] = value
    return mapped


def _unique_key(existing: JsonDict, key: str) -> str:
    if key not in existing:
        return key
    index = 2
    while f"{key}_{index}" in existing:
        index += 1
    return f"{key}_{index}"


def extract_selects(value: str | HtmlNode | None, *, include_disabled: bool = True) -> list[JsonDict]:
    """Extract select controls and their options from an HTML fragment."""
    root = _coerce_html_node(value)
    selects: list[JsonDict] = []
    for select in root.iter("select"):
        options = _select_options(select, include_disabled=include_disabled)
        selects.append(
            {
                "name": clean_text(select.get("name")),
                "id": clean_text(select.get("id")),
                "options": options,
                "disabled": "disabled" in select.attrs,
            }
        )
    return selects


def extract_select_options(
    value: str | HtmlNode | None,
    *,
    name: str | None = None,
    select_id: str | None = None,
    include_disabled: bool = True,
) -> list[JsonDict]:
    """Return options for the first select matching ``name`` or ``select_id``."""
    root = _coerce_html_node(value)
    for select in root.iter("select"):
        if name is not None and select.get("name") != name:
            continue
        if select_id is not None and select.get("id") != select_id:
            continue
        return _select_options(select, include_disabled=include_disabled)
    return []


def _select_options(select: HtmlNode, *, include_disabled: bool) -> list[JsonDict]:
    options: list[JsonDict] = []
    select_disabled = "disabled" in select.attrs
    for option in select.iter("option"):
        optgroup = _nearest_ancestor(option, "optgroup")
        disabled = select_disabled or "disabled" in option.attrs or (optgroup is not None and "disabled" in optgroup.attrs)
        if disabled and not include_disabled:
            continue
        label = option.text()
        raw_value = option.get("value")
        value = _clean_attr_value(raw_value) if raw_value is not None else label or ""
        item: JsonDict = {
            "value": value,
            "label": label,
            "selected": "selected" in option.attrs,
            "disabled": disabled,
        }
        if optgroup is not None:
            item["group"] = clean_text(optgroup.get("label"))
        options.append(item)
    return options


def classify_link(url: str | None, *, base_url: str | None = None, content_type: str | None = None) -> str:
    """Classify a link as pdf, document, spreadsheet, page, external, etc."""
    raw = _clean_attr_value(url) if url is not None else None
    if not raw:
        return "unknown"

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if raw.startswith("#"):
        return "anchor"
    if scheme in _UNSAFE_SCHEMES:
        return "unsafe"
    if scheme == "mailto":
        return "email"
    if scheme == "tel":
        return "phone"

    content_kind = _kind_from_content_type(content_type)
    if content_kind:
        return content_kind

    extension_kind = _kind_from_extension(parsed.path)
    if extension_kind:
        return extension_kind

    resolved = absolute_url(base_url, raw)
    if resolved:
        resolved_host = urlparse(resolved).netloc.lower()
        base_host = urlparse(base_url or "").netloc.lower()
        if base_host and resolved_host and resolved_host != base_host:
            return "external"
        return "page"

    if scheme in {"http", "https"}:
        return "page"
    if not scheme:
        return "relative"
    return "unknown"


def _kind_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    return _CONTENT_TYPE_KINDS.get(normalized)


def _kind_from_extension(path: str) -> str | None:
    lowered = path.lower()
    for extension, kind in _EXTENSION_KINDS.items():
        if lowered.endswith(extension):
            return kind
    return None


def extract_links(value: str | HtmlNode | None, *, base_url: str | None = None, include_unsafe: bool = False) -> list[JsonDict]:
    """Extract anchor links with absolute URLs where possible and a link kind."""
    root = _coerce_html_node(value)
    links: list[JsonDict] = []
    for anchor in root.iter("a"):
        href = _clean_attr_value(anchor.get("href")) if anchor.get("href") is not None else None
        if not href:
            continue
        kind = classify_link(href, base_url=base_url)
        if kind == "unsafe" and not include_unsafe:
            continue

        resolved = absolute_url(base_url, href)
        if resolved is None and kind in {"email", "phone", "relative"}:
            resolved = href
        if resolved is None:
            continue

        item: JsonDict = {
            "url": resolved,
            "label": anchor.text() or resolved,
            "kind": kind,
        }
        if href != resolved:
            item["href"] = href
        links.append(item)
    return links


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


parse_date = parse_argentine_date

__all__ = [
    "HtmlNode",
    "JsonDict",
    "absolute_url",
    "classify_link",
    "clean_snippet",
    "clean_text",
    "ensure_list",
    "extract_links",
    "extract_select_options",
    "extract_selects",
    "extract_table_rows",
    "extract_tables",
    "normalize_date",
    "parse_argentine_date",
    "parse_date",
    "parse_html",
    "text_content",
]
