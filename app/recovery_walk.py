"""Directory walking with a limit enforced while reading directory entries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator


class WalkLimitError(ValueError):
    pass


def bounded_walk(
    root: Path,
    *,
    max_entries: int,
    onerror: Callable[[OSError], None] | None = None,
) -> Iterator[tuple[Path, list[str], list[str]]]:
    """Match top-down, non-link-following walk semantics without unbounded lists."""
    pending = [root]
    scanned = 0
    while pending:
        current = pending.pop()
        directories: list[str] = []
        filenames: list[str] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > max_entries:
                        raise WalkLimitError(
                            "Recovery-Verzeichnis überschreitet das Scanlimit"
                        )
                    try:
                        is_directory = entry.is_dir()
                    except OSError:
                        is_directory = False
                    (directories if is_directory else filenames).append(entry.name)
        except OSError as exc:
            if onerror is not None:
                onerror(exc)
            continue
        yield current, directories, filenames
        # Callers may sort the bounded lists before choosing traversal order.
        pending.extend(
            current / name
            for name in reversed(directories)
            if not (current / name).is_symlink()
        )
