from app.jobs.pair_planner import execution_waves, has_overlapping_pairs, pairs_conflict


def _pair(name: str, local: str, remote: str) -> dict:
    return {"name": name, "local": local, "remote": remote}


def test_only_conflicting_pairs_are_serialized():
    pairs = [
        _pair("A", "/srv/a", "cloud:a"),
        _pair("B", "/srv/a/child", "cloud:b"),
        _pair("C", "/srv/c", "cloud:c"),
    ]
    waves = execution_waves(pairs, max_parallel=3)
    assert [[pair["name"] for pair in wave] for wave in waves] == [["A", "C"], ["B"]]
    assert has_overlapping_pairs(pairs)


def test_worker_limit_is_respected_without_conflicts():
    pairs = [_pair(str(i), f"/srv/{i}", f"cloud:{i}") for i in range(5)]
    assert [len(wave) for wave in execution_waves(pairs, 2)] == [2, 2, 1]


def test_remote_overlap_is_detected_across_endpoint_roles_and_slash_spelling():
    first = _pair("A", "/srv/source-a", "Cloud:/shared")
    second = _pair("B", "/srv/source-b", "cloud:shared/child")
    third = _pair("C", "/srv/source-c", "other:shared")

    assert pairs_conflict(first, second) is True
    assert pairs_conflict(first, third) is False
