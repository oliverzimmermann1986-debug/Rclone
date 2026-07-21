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
        aa, bb = a.rstrip("/"), b.rstrip("/")
        return aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/")
    try:
        pa, pb = Path(a).resolve(), Path(b).resolve()
        return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)
    except (OSError, RuntimeError, ValueError):
        return False


def pairs_conflict(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return paths_overlap(
        str(first.get("local") or ""), str(second.get("local") or "")
    ) or paths_overlap(str(first.get("remote") or ""), str(second.get("remote") or ""))


def has_overlapping_pairs(pairs: list[dict[str, Any]]) -> bool:
    return any(
        pairs_conflict(first, second)
        for index, first in enumerate(pairs)
        for second in pairs[index + 1 :]
    )


def execution_waves(
    pairs: list[dict[str, Any]], max_parallel: int
) -> list[list[dict[str, Any]]]:
    """Group pairs into bounded waves whose members do not conflict."""
    width = max(1, int(max_parallel))
    waves: list[list[dict[str, Any]]] = []
    for pair in pairs:
        for wave in waves:
            if len(wave) < width and not any(
                pairs_conflict(pair, item) for item in wave
            ):
                wave.append(pair)
                break
        else:
            waves.append([pair])
    return waves
