"""Regression tests for ``legal.dispatch._params_to_argv``.

A param dict is keyed by each action's ``dest``, which is not always the option
spelling — e.g. the document-text ``--text`` flag has ``dest="want_text"``. The
MCP document-text resolver passes ``want_text=True``; if argv synthesis assumes
``--{key}`` it emits an unrecognized ``--want-text`` and the operation aborts
with ``unrecognized arguments: --want-text``. These tests pin the dest→option
resolution so that regression can't return.
"""

from __future__ import annotations

from legal.cli import JsonArgumentParser, _add_source_shared_flags
from legal.dispatch import _params_to_argv, resolve_operation


def _parser_for(source_id: str, operation: str) -> JsonArgumentParser:
    op = resolve_operation(source_id, operation)
    parser = JsonArgumentParser(add_help=False)
    _add_source_shared_flags(parser)
    if op.add_arguments is not None:
        op.add_arguments(parser)
    return parser


def test_want_text_maps_to_text_flag() -> None:
    parser = _parser_for("saij", "download")
    argv = _params_to_argv(parser, {"guid": "X", "want_text": True})

    # The dest is want_text but the option spelling is --text.
    assert "--text" in argv
    assert "--want-text" not in argv
    # And it parses cleanly, setting the dest the handler reads.
    ns = parser.parse_args(argv)
    assert ns.want_text is True
    assert ns.guid == "X"


def test_falsey_store_true_is_omitted() -> None:
    parser = _parser_for("saij", "download")
    argv = _params_to_argv(parser, {"guid": "X", "want_text": False})
    assert "--text" not in argv
    assert "--want-text" not in argv


def test_unknown_key_falls_back_to_dashed_option() -> None:
    parser = _parser_for("saij", "download")
    # A key with no matching action keeps the legacy --dashed spelling so the
    # parser surfaces a real "unrecognized arguments" rather than silently
    # dropping it.
    argv = _params_to_argv(parser, {"no_such_flag": "v"})
    assert argv == ["--no-such-flag", "v"]
