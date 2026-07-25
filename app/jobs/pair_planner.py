"""Plan parallel execution without running overlapping sync pairs together."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def is_remote(path: str) -> bool:
    return bool(
        path
        and not Path(path).is_absolute()
        and not path.startswith("-")
        and ":" in path
    )


def paths_overlap(a: str, b: str) -> bool:
    if not a or not b or is_remote(a) != is_remote(b):
        return False
    if is_remote(a):

        def canonical_remote(value: str) -> str:
            remote, _, raw_path = value.partition(":")
            path = "/" + raw_path.lstrip("/")
            return f"{remote.casefold()}:{path.rstrip('/')}"

        aa, bb = canonical_remote(a), canonical_remote(b)
        return aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/")
    try:
        pa, pb = Path(a).resolve(), Path(b).resolve()
        return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)
    except (OSError, RuntimeError, ValueError):
        # Unklare Ressourcenidentität wird sicherheitshalber seriell geplant.
        return True


def pairs_conflict(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return any(
        paths_overlap(
            str(first.get(first_key) or ""), str(second.get(second_key) or "")
        )
        for first_key in ("local", "remote")
        for second_key in ("local", "remote")
    )


def has_overlapping_pairs(pairs: list[dict[str, Any]]) -> bool:
    return any(
        pairs_conflict(first, second)
        for index, first in enumerate(pairs)
        for second in pairs[index + 1 :]
    )

