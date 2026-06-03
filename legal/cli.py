"""Command-line interface for portable Argentina legal source tools."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Sequence

from legal.errors import LegalCliError, usage_error
from legal.models import (
    LegalDocument,
    LegalItem,
    LegalResponse,
    Provenance,
)
from legal.registry import REQUIRES_SEARCH_FILTERS_CAPABILITY, SOURCES, SOURCE_IDS, list_sources
from legal.schema import LEGAL_RESPONSE_SCHEMA
from legal.sources import default_operation, get_adapter
from legal.sources.base import SourceOperation


class JsonArgumentParser(argparse.ArgumentParser):
    """Argparse parser that reports usage failures as JSON-ready errors."""

    def error(self, message: str) -> None:
        raise usage_error(
            message,
            details={"usage": self.format_usage().strip()},
        )


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def emit_json(payload: Any, *, pretty: bool = False) -> None:
    """Write one JSON document to stdout."""
    kwargs = {"ensure_ascii": False, "default": _json_default}
    if pretty:
        kwargs.update({"indent": 2, "sort_keys": True})
    print(json.dumps(payload, **kwargs))


def emit_error(
    error: LegalCliError,
    *,
    pretty: bool = False,
    source: str = "legal",
    operation: str = "usage",
) -> None:
    """Write a normalized handled-error response to stdout."""
    emit_json(error.to_response(source=source, operation=operation), pretty=pretty)


def _error_exit_code(error: LegalCliError) -> int:
    return 2 if error.code == "usage_error" else 1


def cmd_sources(args: argparse.Namespace) -> int:
    emit_json(
        LegalResponse.search(
            source="legal",
            operation="sources",
            query={},
            items=[],
            provenance=Provenance(source_map="legal/registry.py"),
        ).to_dict()
        | {"items": list_sources()},
        pretty=args.pretty,
    )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    document = LegalDocument(
        id="legal-cli-response-schema",
        title="Legal CLI response schema",
        document_type="json_schema",
        content_type="application/schema+json",
        metadata={"schema": LEGAL_RESPONSE_SCHEMA},
        provenance=Provenance(source_map="legal/schema.py"),
    )
    emit_json(
        LegalResponse.document_response(
            source="legal",
            operation="schema",
            request={},
            document=document,
            provenance=document.provenance,
        ),
        pretty=args.pretty,
    )
    return 0


def cmd_source_operation(args: argparse.Namespace) -> int:
    _normalize_source_shared_args(args)
    operation = args.source_operation
    result = operation.handler(args)
    emit_json(result, pretty=args.pretty)
    if isinstance(result, LegalResponse):
        return 0 if result.ok else 1
    if isinstance(result, Mapping):
        return 0 if result.get("ok", True) else 1
    return 0


def cmd_global_search(args: argparse.Namespace) -> int:
    from legal.global_search import run_global_search

    response = run_global_search(
        text=args.text,
        sources=list(args.global_sources or []),
        all_direct=bool(args.all_direct),
        limit_per_source=args.limit_per_source,
        raw=bool(args.raw),
    )
    emit_json(response, pretty=args.pretty)
    return 0 if response.ok else 1


def cmd_missing_source_operation(args: argparse.Namespace) -> int:
    raise usage_error(
        "operation is required",
        details={"source": args.source_id, "usage": args.source_usage},
    )


def _add_pretty(parser: argparse.ArgumentParser, *, default: bool | str = False) -> None:
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=default,
        help="pretty-print JSON output",
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to 1")
    return parsed


def _add_source_shared_flags(
    parser: argparse.ArgumentParser,
    *,
    default: Any = argparse.SUPPRESS,
    dest_prefix: str = "",
) -> None:
    parser.add_argument(
        "--limit",
        dest=f"{dest_prefix}limit",
        type=_positive_int,
        default=default,
        help="maximum number of records to return",
    )
    parser.add_argument(
        "--cursor",
        dest=f"{dest_prefix}cursor",
        default=default,
        help="stateless pagination cursor from a prior response",
    )
    parser.add_argument(
        "--search-id",
        dest=f"{dest_prefix}search_id",
        default=default,
        help="stateful search id from a prior response",
    )
    parser.add_argument(
        "--raw",
        dest=f"{dest_prefix}raw",
        action="store_true",
        default=default,
        help="include raw source fields when the adapter supports them",
    )


def _normalize_source_shared_args(args: argparse.Namespace) -> None:
    for name, fallback in {
        "limit": None,
        "cursor": None,
        "search_id": None,
        "raw": False,
    }.items():
        if hasattr(args, name):
            continue
        setattr(args, name, getattr(args, f"source_{name}", fallback))


def _source_operation(source_id: str, operation: str) -> SourceOperation:
    adapter = get_adapter(source_id)
    source = next(source for source in SOURCES if source.id == source_id)
    if adapter is None:
        return default_operation(source, operation)
    return adapter.get_operation(operation) or default_operation(source, operation)


# Sources that have no generic ``search`` op but expose a search-like operation
# the global fan-out can route to (e.g. csjn -> fallos).
_GLOBAL_SEARCH_OP_ALIASES = {"csjn": "fallos"}

# Browser-backed sources cheap enough (no captcha-solver credits) to fold into
# the broad ``all_direct`` search, so Corte Suprema jurisprudence is never hidden
# from a cross-source query.
_GLOBAL_SEARCH_BROWSER_INCLUDE = {"csjn"}

# Sources whose search spends captcha-solver credits and launches a browser per
# call (ptn = invisible reCAPTCHA via Capsolver), kept out of the broad
# ``all_direct`` fan-out to bound its cost/latency. They remain available when
# listed explicitly in ``sources``.
_GLOBAL_SEARCH_ALL_DIRECT_EXCLUDE = {"ptn"}


# Official "Fallos" citation, volume:page (e.g. "327:327", "Fallos 272:188").
# Anchored to a 2-3 digit volume + 1-4 digit page so it does not match ordinary
# numbers; CSJN sumarios index the citation, so such queries route there.
_FALLOS_CITATION_RE = re.compile(r"\b(?:fallos?\s*)?\d{2,3}\s*:\s*\d{1,4}\b", re.IGNORECASE)


def _is_fallos_citation(text: str | None) -> bool:
    return bool(text and _FALLOS_CITATION_RE.search(text))


def _global_search_op_name(source_id: str, text: str = "") -> str | None:
    """Return the operation the global fan-out should call for *source_id*.

    ``"search"`` when the source exposes it; for ``csjn`` a Fallos-citation
    query (volume:page) routes to ``sumarios`` (which index the citation) and
    everything else to ``fallos``; the configured alias otherwise. ``None`` when
    the source cannot participate (skipped with a warning by the caller).
    """
    source = next((s for s in SOURCES if s.id == source_id), None)
    if source is None:
        return None
    if "search" in source.operations:
        return "search"
    if source_id == "csjn":
        if _is_fallos_citation(text) and "sumarios" in source.operations:
            return "sumarios"
        if "fallos" in source.operations:
            return "fallos"
    alias = _GLOBAL_SEARCH_OP_ALIASES.get(source_id)
    if alias and alias in source.operations:
        return alias
    return None


def _select_global_search_sources(args: argparse.Namespace) -> list[str]:
    if args.all_direct:
        selected: list[str] = []
        for source in SOURCES:
            if REQUIRES_SEARCH_FILTERS_CAPABILITY in source.capabilities:
                continue
            if _global_search_op_name(source.id) is None:
                continue
            if source.id in _GLOBAL_SEARCH_ALL_DIRECT_EXCLUDE:
                continue
            if source.browser_required and source.id not in _GLOBAL_SEARCH_BROWSER_INCLUDE:
                continue
            selected.append(source.id)
        return selected
    selected: list[str] = []
    for source_id in args.global_sources or []:
        if source_id not in SOURCE_IDS:
            raise usage_error(
                f"unknown source: {source_id}",
                details={"source": source_id, "known_sources": list(SOURCE_IDS)},
            )
        # Non-searchable sources are no longer rejected here: the fan-out skips
        # them gracefully and reports them in facets.source_errors, so one
        # unsearchable source never aborts the whole search.
        if source_id not in selected:
            selected.append(source_id)
    if not selected:
        raise usage_error("at least one --source or --all-direct is required")
    return selected


def _build_global_source_search_args(
    *,
    source_id: str,
    operation: SourceOperation,
    text: str,
    limit: int,
    raw: bool,
) -> argparse.Namespace:
    parser = JsonArgumentParser(add_help=False)
    _add_source_shared_flags(parser)
    if operation.add_arguments is not None:
        operation.add_arguments(parser)
    argv = ["--limit", str(limit)]
    text_option = _source_text_option(parser)
    if text_option is not None:
        argv.extend([text_option, text])
    if raw:
        argv.append("--raw")
    parsed = parser.parse_args(argv)
    if not hasattr(parsed, "text"):
        parsed.text = text
    if not hasattr(parsed, "words"):
        parsed.words = text
    parsed.pretty = False
    parsed.source = source_id
    parsed.source_id = source_id
    parsed.operation = "search"
    parsed.source_operation = operation
    _normalize_source_shared_args(parsed)
    return parsed


def _source_text_option(parser: argparse.ArgumentParser) -> str | None:
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    for option in ("--text", "--texto", "--q", "--words", "--phrase"):
        if option in options:
            return option
    return None


def _result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, LegalResponse):
        return result.to_dict()
    if isinstance(result, Mapping):
        return dict(result)
    return {"ok": True}


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tag_global_item(item: Any, source_id: str) -> Any:
    if isinstance(item, LegalItem):
        source_fields = dict(item.source_fields)
        source_fields.setdefault("source", source_id)
        source_fields.setdefault("legal_source", source_id)
        return replace(item, source_fields=source_fields)
    if isinstance(item, Mapping):
        tagged = dict(item)
        source_fields = _mapping_or_empty(tagged.get("source_fields"))
        source_fields.setdefault("source", source_id)
        source_fields.setdefault("legal_source", source_id)
        tagged["source_fields"] = source_fields
        return tagged
    return item


def _extend_unique(target: list[str], values: Any) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        if isinstance(value, str) and value not in target:
            target.append(value)


def _add_source_commands(
    subparsers: Any,
    *,
    pretty_parent: argparse.ArgumentParser,
    source_shared_parent: argparse.ArgumentParser,
    operation_shared_parent: argparse.ArgumentParser,
) -> None:
    for source in SOURCES:
        source_parser = subparsers.add_parser(
            source.id,
            parents=[pretty_parent, source_shared_parent],
            help=source.name,
            description=f"{source.name} source operations.",
        )
        source_parser.set_defaults(
            func=cmd_missing_source_operation,
            source=source.id,
            source_id=source.id,
            operation="usage",
        )
        operation_subparsers = source_parser.add_subparsers(
            dest="operation_command",
            parser_class=JsonArgumentParser,
        )
        for operation_name in source.operations:
            operation = _source_operation(source.id, operation_name)
            operation_parser = operation_subparsers.add_parser(
                operation_name,
                parents=[pretty_parent, operation_shared_parent],
                help=operation.help or f"{operation_name} {source.name}",
            )
            if operation.add_arguments is not None:
                operation.add_arguments(operation_parser)
            operation_parser.set_defaults(
                func=cmd_source_operation,
                source=source.id,
                source_id=source.id,
                operation=operation_name,
                source_operation=operation,
            )
        source_parser.set_defaults(source_usage=source_parser.format_usage().strip())


def build_parser() -> argparse.ArgumentParser:
    pretty_parent = JsonArgumentParser(add_help=False)
    _add_pretty(pretty_parent, default=argparse.SUPPRESS)
    source_shared_parent = JsonArgumentParser(add_help=False)
    _add_source_shared_flags(source_shared_parent, dest_prefix="source_")
    operation_shared_parent = JsonArgumentParser(add_help=False)
    _add_source_shared_flags(operation_shared_parent)

    parser = JsonArgumentParser(
        prog="python -m legal.cli",
        description="Portable JSON CLI for Argentina legal sources.",
    )
    _add_pretty(parser)

    subparsers = parser.add_subparsers(dest="command", parser_class=JsonArgumentParser)

    sources = subparsers.add_parser(
        "sources",
        parents=[pretty_parent],
        help="list configured legal sources",
    )
    sources.set_defaults(func=cmd_sources)

    schema = subparsers.add_parser(
        "schema",
        parents=[pretty_parent],
        help="print the legal CLI response schema",
    )
    schema.set_defaults(func=cmd_schema)

    global_search = subparsers.add_parser(
        "search",
        parents=[pretty_parent],
        help="search across multiple direct legal sources",
    )
    global_source_selector = global_search.add_mutually_exclusive_group(required=True)
    global_source_selector.add_argument(
        "--source",
        dest="global_sources",
        action="append",
        choices=SOURCE_IDS,
        help="source id to include; may be repeated",
    )
    global_source_selector.add_argument(
        "--all-direct",
        action="store_true",
        help="search every non-browser source with a search operation",
    )
    global_search.add_argument(
        "--text",
        "--q",
        dest="text",
        required=True,
        help="free-text search",
    )
    global_search.add_argument(
        "--limit-per-source",
        type=_positive_int,
        required=True,
        help="maximum records to request from each source",
    )
    global_search.add_argument(
        "--raw",
        action="store_true",
        default=False,
        help="request raw source fields from adapters that support them",
    )
    global_search.set_defaults(func=cmd_global_search, source="legal", operation="search")

    _add_source_commands(
        subparsers,
        pretty_parent=pretty_parent,
        source_shared_parent=source_shared_parent,
        operation_shared_parent=operation_shared_parent,
    )

    return parser


def _has_pretty(argv: Sequence[str] | None) -> bool:
    args = sys.argv[1:] if argv is None else argv
    return "--pretty" in args


def _error_context(args: argparse.Namespace | None) -> tuple[str, str]:
    if args is None:
        return "legal", "usage"
    source = getattr(args, "source", None) or getattr(args, "source_id", None) or "legal"
    operation = getattr(args, "operation", None) or getattr(args, "command", None) or "usage"
    return source, operation or "usage"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = None
    pretty = _has_pretty(argv)
    try:
        args = parser.parse_args(argv)
        pretty = args.pretty
        if not hasattr(args, "func"):
            raise usage_error("command is required", details={"usage": parser.format_usage().strip()})
        return args.func(args)
    except LegalCliError as error:
        source, operation = _error_context(args)
        emit_error(error, pretty=pretty, source=source, operation=operation)
        return _error_exit_code(error)
    except BrokenPipeError:
        # A downstream consumer (head, jq, a pipe) closed the read end early.
        # Suppress the secondary "Exception ignored" flush at interpreter exit by
        # redirecting stdout to the null device, then exit quietly.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except Exception as exc:
        # Contract guard: a handler raised something unexpected (e.g. a browser
        # or network library error escaping a browser-backed source). Agents rely
        # on always receiving one JSON envelope, so convert it into a normalized,
        # retryable error instead of leaking a raw traceback to stderr.
        source, operation = _error_context(args)
        error = LegalCliError(
            code="source_unavailable",
            message=f"{source} {operation} failed unexpectedly: {type(exc).__name__}: {exc}"[:400],
            retryable=True,
            details={"exception_type": type(exc).__name__, "unexpected": True},
        )
        emit_error(error, pretty=pretty, source=source, operation=operation)
        return 1


if __name__ == "__main__":
    sys.exit(main())
