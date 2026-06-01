"""Source adapter primitives for legal CLI dispatch."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from legal.models import LegalResponse, SourceInfo


CommandResult = LegalResponse | Mapping[str, Any]
CommandHandler = Callable[[argparse.Namespace], CommandResult]
ArgumentRegistrar = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class SourceOperation:
    """A source operation plus its argparse extension hook and handler."""

    name: str
    handler: CommandHandler
    help: str | None = None
    add_arguments: ArgumentRegistrar | None = None


class SourceAdapter:
    """CLI adapter for one legal source."""

    def __init__(self, source_info: SourceInfo) -> None:
        self.source_info = source_info
        self._operations: dict[str, SourceOperation] = {}

    @property
    def source_id(self) -> str:
        return self.source_info.id

    def register_operation(
        self,
        name: str,
        handler: CommandHandler,
        *,
        help: str | None = None,
        add_arguments: ArgumentRegistrar | None = None,
    ) -> SourceOperation:
        if name not in self.source_info.operations:
            raise ValueError(f"{name!r} is not declared for source {self.source_id!r}")
        operation = SourceOperation(
            name=name,
            handler=handler,
            help=help,
            add_arguments=add_arguments,
        )
        self._operations[name] = operation
        return operation

    def get_operation(self, name: str) -> SourceOperation | None:
        return self._operations.get(name)
