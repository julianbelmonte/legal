"""Thin launcher for the legal CLI from inside the app folder."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence


def _ensure_repo_root_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    launcher_dir = Path(__file__).resolve().parent

    # Avoid sibling modules such as http.py shadowing standard library packages.
    filtered_path = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except (OSError, RuntimeError):
            filtered_path.append(entry)
            continue
        if resolved in {repo_root, launcher_dir}:
            continue
        filtered_path.append(entry)

    sys.path[:] = [str(repo_root), *filtered_path]


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_repo_root_on_path()
    from apps.legal import cli

    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
