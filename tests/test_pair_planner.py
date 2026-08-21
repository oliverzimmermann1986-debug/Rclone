from app.jobs.pair_planner import has_overlapping_pairs, pairs_conflict, paths_overlap
from app.jobs.rclone_sync import _next_runnable_pair_index


def _pair(name: str, local: str, remote: str) -> dict:
    return {"name": name, "local": local, "remote": remote}


def test_only_conflicting_pairs_are_serialized():
    pairs = [
        _pair("A", "/srv/a", "cloud:a"),
        _pair("B", "/srv/a/child", "cloud:b"),
        _pair("C", "/srv/c", "cloud:c"),
    ]
    # B kollidiert mit A, C ist unabhängig: solange A läuft, ist C der nächste
    # startbare Kandidat, B wartet.
    assert _next_runnable_pair_index(pairs[1:], [pairs[0]]) == 1
    assert _next_runnable_pair_index([pairs[1]], [pairs[0]]) is None
    assert has_overlapping_pairs(pairs)


def test_worker_limit_is_respected_without_conflicts():
    pairs = [_pair(str(i), f"/srv/{i}", f"cloud:{i}") for i in range(5)]
    # Ohne Konflikte ist immer der erste Kandidat startbar; die Obergrenze
    # kommt allein aus max_parallel im Runner.
    assert _next_runnable_pair_index(pairs, []) == 0
    assert _next_runnable_pair_index(pairs, pairs[:2]) == 2


def test_remote_overlap_is_detected_across_endpoint_roles_and_slash_spelling():
    first = _pair("A", "/srv/source-a", "Cloud:/shared")
    second = _pair("B", "/srv/source-b", "cloud:shared/child")
    third = _pair("C", "/srv/source-c", "other:shared")

    assert pairs_conflict(first, second) is True
    assert pairs_conflict(first, third) is False


def test_local_overlap_is_fail_closed_across_case_variants():
    assert paths_overlap("/srv/Photos", "/srv/photos") is True
    assert paths_overlap("/srv/Photos", "/srv/PHOTOS/2026") is True
    assert paths_overlap("/srv/Photos", "/srv/Photographs") is False
