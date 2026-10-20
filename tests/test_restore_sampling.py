import itertools
import random

import pytest

from app.jobs.restore_sampling import select_budgeted_sample


def _greedy(candidates, count, budget):
    selected = []
    used = 0
    for path, size in candidates:
        if used + size <= budget:
            selected.append((path, size))
            used += size
            if len(selected) == count:
                break
    return selected


def test_replaces_large_choice_to_reach_twenty_within_256_mib():
    mib = 1024 * 1024
    candidates = [("video.mov", 112 * mib)] + [
        (f"photo-{index}.jpg", 8 * mib) for index in range(20)
    ]
    budget = 256 * mib
    assert len(_greedy(candidates, 20, budget)) == 19

    result = select_budgeted_sample(candidates, 20, budget)

    assert result == candidates[1:]
    assert len(result) == 20
    assert sum(size for _, size in result) <= budget


def test_preserves_existing_random_greedy_when_requested_count_fits_exactly():
    candidates = [("z", 80), ("b", 10), ("a", 10), ("tiny", 1)]
    assert select_budgeted_sample(candidates, 3, 100) == candidates[:3]


def test_fallback_can_reach_an_exact_budget_fit():
    candidates = [("large", 9), ("first", 4), ("second", 3), ("third", 3)]
    assert select_budgeted_sample(candidates, 3, 10) == candidates[1:]


def test_returns_maximal_count_when_requested_count_cannot_fit():
    candidates = [("large", 9), ("second", 5), ("third", 4), ("fourth", 3)]
    result = select_budgeted_sample(candidates, 4, 10)
    assert len(result) == 2
    assert sum(size for _, size in result) <= 10


def test_keeps_greedy_choice_when_replacement_would_not_improve_count():
    candidates = [("earlier-random-large", 8), ("later-small", 4)]
    assert select_budgeted_sample(candidates, 20, 10) == candidates[:1]


def test_fewer_eligible_candidates_preserve_input_order():
    candidates = [("z", 3), ("a", 4), ("oversized", 100), ("b", 2)]
    assert select_budgeted_sample(candidates, 20, 10) == [
        ("z", 3),
        ("a", 4),
        ("b", 2),
    ]


def test_zero_byte_files_fit_a_zero_byte_budget():
    candidates = [("z", 0), ("nonempty", 1), ("b", 0), ("a", 0)]
    assert select_budgeted_sample(candidates, 2, 0) == [("z", 0), ("b", 0)]
    assert select_budgeted_sample(candidates, 20, 0) == [
        ("z", 0),
        ("b", 0),
        ("a", 0),
    ]


def test_rejects_unknown_negative_fractional_boolean_and_oversized_sizes():
    candidates = [
        ("unknown", None),
        ("negative", -1),
        ("nan", float("nan")),
        ("infinite", float("inf")),
        ("float", 1.0),
        ("fraction", 0.1),
        ("string", "1"),
        ("boolean", True),
        ("huge", 10**100),
        ("oversized", 11),
        ("valid", 10),
    ]
    assert select_budgeted_sample(candidates, 20, 10) == [("valid", 10)]


def test_integer_byte_arithmetic_does_not_round_large_values():
    candidates = [("large", 2**63), ("one", 1), ("empty", 0)]
    assert select_budgeted_sample(candidates, 2, 2**63) == [
        ("large", 2**63),
        ("empty", 0),
    ]
    assert select_budgeted_sample(candidates, 3, 2**63 + 1) == candidates


def test_duplicate_paths_use_first_valid_occurrence_only():
    candidates = [
        ("same", None),
        ("same", 4),
        ("same", 1),
        ("other", 3),
        ("other", 3),
        ("empty", 0),
    ]
    assert select_budgeted_sample(candidates, 20, 10) == [
        ("same", 4),
        ("other", 3),
        ("empty", 0),
    ]


def test_ignores_malformed_candidates_without_mutating_input():
    candidates = [None, (), ("three", 1, 2), ("", 0), (None, 0), ["valid", 2]]
    original = list(candidates)
    assert select_budgeted_sample(candidates, 20, 3) == [("valid", 2)]
    assert candidates == original


@pytest.mark.parametrize("sample_size,budget", [(0, 10), (-1, 10), (5, -1)])
def test_nonpositive_count_or_negative_budget_selects_nothing(sample_size, budget):
    assert select_budgeted_sample([("empty", 0)], sample_size, budget) == []


@pytest.mark.parametrize(
    "sample_size,budget", [(True, 1), (1.0, 1), (1, True), (1, 1.0), (1, None)]
)
def test_invalid_limit_types_fail_without_coercion(sample_size, budget):
    with pytest.raises(ValueError, match="integers"):
        select_budgeted_sample([("a", 0)], sample_size, budget)


def test_seeded_reservoir_order_is_deterministic_and_changes_with_permutation():
    original = [(f"path-{index:02d}", 1) for index in range(30)]
    first = list(original)
    repeated = list(original)
    different = list(original)
    random.Random(42).shuffle(first)
    random.Random(42).shuffle(repeated)
    random.Random(43).shuffle(different)

    selected = select_budgeted_sample(first, 20, 20)
    assert selected == select_budgeted_sample(repeated, 20, 20)
    assert selected == first[:20]
    assert selected != select_budgeted_sample(different, 20, 20)
    assert first != sorted(first)


def test_fallback_ties_retain_earlier_random_positions_not_path_order():
    candidates = [("z", 7), ("a", 7), ("later-one", 2), ("later-two", 1)]
    assert select_budgeted_sample(candidates, 3, 10) == [
        ("z", 7),
        ("later-one", 2),
        ("later-two", 1),
    ]


def test_bounded_random_cases_match_exhaustive_maximum_cardinality():
    rng = random.Random(20260909)
    for _ in range(250):
        candidates = [
            (f"file-{index}", rng.randrange(0, 15))
            for index in range(rng.randrange(0, 9))
        ]
        rng.shuffle(candidates)
        requested = rng.randrange(1, 10)
        budget = rng.randrange(0, 35)
        expected = max(
            (
                len(subset)
                for count in range(min(requested, len(candidates)) + 1)
                for subset in itertools.combinations(candidates, count)
                if sum(size for _, size in subset) <= budget
            ),
            default=0,
        )

        result = select_budgeted_sample(candidates, requested, budget)

        assert len(result) == expected
        assert sum(size for _, size in result) <= budget
        assert len({path for path, _ in result}) == len(result)
        assert all(item in candidates for item in result)
        assert result == [item for item in candidates if item in result]
