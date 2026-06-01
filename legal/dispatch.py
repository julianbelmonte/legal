"""Uniform, source-agnostic dispatch seam.

This module exposes the single agnostic accessor used by non-CLI consumers
(the FastAPI API, and a future MCP server) to run any registered CLI operation
from a plain parameter dictionary, returning the same normalized envelope the
CLI produces.

It deliberately reuses the private helpers in :mod:`legal.cli`
(``_add_source_shared_flags``, ``_normalize_source_shared_args``,
``JsonArgumentParser``, ``_source_operation``) so the dispatch path never
diverges from the CLI's argument-handling behavior.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from legal.cli import (
    JsonArgumentParser,
    _add_source_shared_flags,
    _normalize_source_shared_args,
    _source_operation,
)
from legal.errors import usage_error
from legal.models import LegalResponse
from legal.registry import SOURCES, SOURCE_IDS
from legal.sources.base import SourceOperation

__all__ = ["resolve_operation", "run_operation"]


def resolve_operation(source_id: str, operation: str) -> SourceOperation:
    """Resolve the concrete :class:`SourceOperation` for a source/op pair.

    Mirrors ``cli._source_operation`` (registry adapter → ``get_operation`` or
    ``default_operation``) but validates the source and operation first, raising
    a ``usage_error`` (``LegalCliError``) for unknown source/operation so callers
    can convert it to the normalized error envelope.
    """
    if source_id not in SOURCE_IDS:
        raise usage_error(
            f"unknown source: {source_id}",
            details={"source": source_id, "known_sources": list(SOURCE_IDS)},
        )
    source = next(source for source in SOURCES if source.id == source_id)
    if operation not in source.operations:
        raise usage_error(
            f"unknown operation: {operation}",
            details={
                "source": source_id,
                "operation": operation,
                "known_operations": list(source.operations),
            },
        )
    return _source_operation(source_id, operation)


def _params_to_argv(parser: argparse.ArgumentParser, params: Mapping[str, Any]) -> list[str]:
    """Translate a params dict into argv understood by ``parser``.

    Booleans map to store_true flags (included only when truthy), ``None`` values
    are skipped, and every other value is emitted as ``--key value``. Underscores
    in keys are normalized to dashes to match the CLI option spelling.
    """
    store_true_dests = {
        action.dest
        for action in parser._actions
        if isinstance(action, argparse._StoreTrueAction)
    }
    argv: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        option = f"--{str(key).replace('_', '-')}"
        dest = str(key).replace("-", "_")
        if dest in store_true_dests or isinstance(value, bool):
            if value:
                argv.append(option)
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([option, str(item)])
            continue
        argv.extend([option, str(value)])
    return argv


def run_operation(
    source_id: str,
    operation: str,
    params: Mapping[str, Any] | None = None,
    *,
    raw: bool = False,
    pretty: bool = False,
) -> LegalResponse | Mapping[str, Any]:
    """Run any registered operation from a params dict and return its result.

    Builds a parser from the operation's ``add_arguments`` (plus the shared
    source flags), synthesizes argv from ``params``, parses, fills the namespace
    fields the handlers rely on, normalizes shared args, and invokes the handler.
    The returned value is the unmodified normalized envelope
    (``LegalResponse`` or a mapping). Bad params raise ``LegalCliError``.
    """
    op = resolve_operation(source_id, operation)

    parser = JsonArgumentParser(add_help=False)
    _add_source_shared_flags(parser)
    if op.add_arguments is not None:
        op.add_arguments(parser)

    effective_params: dict[str, Any] = dict(params or {})
    if raw:
        effective_params.setdefault("raw", True)

    argv = _params_to_argv(parser, effective_params)
    ns = parser.parse_args(argv)

    ns.source = source_id
    ns.source_id = source_id
    ns.operation = operation
    ns.source_operation = op
    ns.pretty = pretty
    _normalize_source_shared_args(ns)

    return op.handler(ns)
