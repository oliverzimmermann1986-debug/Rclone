"""Pure, byte-budgeted selection from an already randomized restore reservoir."""

from __future__ import annotations

import heapq
from collections.abc import Sequence

Candidate = tuple[str, int]


def select_budgeted_sample(
    candidates: Sequence[Candidate], sample_size: int, max_total_bytes: int
) -> list[Candidate]:
    """Select as many distinct files as feasible, up to ``sample_size``.

    The caller supplies its *already shuffled, bounded reservoir*. This helper
    performs no listing, I/O, random draws or path sorting. It cannot guarantee
    the requested count when that reservoir lacks enough affordable files.

    Keep the original order-greedy selection when it already achieves the
    requested or maximum feasible count. Otherwise, a largest-first eviction
    heap finds a feasible higher-count subset: for each processed prefix it
    retains a minimum-size subset of maximum feasible cardinality, stopping as
    soon as the requested count is reached. Equal sizes prefer earlier entries
    in the randomized input. Return order always follows that input.

    This is constrained sampling, not uniform sampling across all feasible
    subsets. Smaller files are favored only if replacing larger choices can
    improve the count. Time is O(n log(min(n, sample_size) + 1)); storage O(n).

    Ignore malformed candidates, empty/non-string paths and sizes that are not
    genuine nonnegative integers within the budget. For duplicate paths, the
    first valid occurrence wins; do not mutate or normalize caller paths.
    Integer nonpositive sample sizes or negative budgets select nothing; a
    zero-byte budget can still select empty files. Invalid limit types raise
    ValueError rather than silently coercing or rounding safety boundaries.
    """
    if type(sample_size) is not int or type(max_total_bytes) is not int:
        raise ValueError("Sample count and byte budget must be integers")
    if sample_size <= 0 or max_total_bytes < 0:
        return []

    eligible: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, (tuple, list)) or len(candidate) != 2:
            continue
        path, size = candidate
        if (
            not isinstance(path, str)
            or not path
            or type(size) is not int
            or size < 0
            or size > max_total_bytes
            or path in seen
        ):
            continue
        seen.add(path)
        eligible.append((path, size))

    greedy: list[Candidate] = []
    greedy_bytes = 0
    for candidate in eligible:
        if greedy_bytes + candidate[1] <= max_total_bytes:
            greedy.append(candidate)
            greedy_bytes += candidate[1]
            if len(greedy) == sample_size:
                return greedy

    # Negative size makes heapq a max-heap. Negative position breaks size ties
    # by evicting the later input occurrence, not by sorting path names.
    selected: list[tuple[int, int]] = []
    selected_bytes = 0
    for index, (_path, size) in enumerate(eligible):
        heapq.heappush(selected, (-size, -index))
        selected_bytes += size
        if selected_bytes > max_total_bytes:
            negative_size, _negative_index = heapq.heappop(selected)
            selected_bytes += negative_size
        if len(selected) == sample_size:
            break

    # Do not favor smaller files when that buys no additional verified file.
    if len(selected) <= len(greedy):
        return greedy
    positions = sorted(-negative_index for _size, negative_index in selected)
    return [eligible[index] for index in positions]
