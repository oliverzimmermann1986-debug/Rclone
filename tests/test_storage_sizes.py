"""Tests für Quelle/Ziel-Auflösung und Dateizahl/Größe in der Storage-Übersicht."""

import threading
import time
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.routes import api_storage


@pytest.fixture(autouse=True)
def _clear_size_cache():
    with api_storage._size_cache_lock:
        api_storage._size_cache.clear()
        api_storage._size_inflight.clear()
    with api_storage._composition_cache_lock:
        api_storage._composition_cache.clear()
        api_storage._composition_inflight.clear()
    yield
    with api_storage._size_cache_lock:
        api_storage._size_cache.clear()
        api_storage._size_inflight.clear()
    with api_storage._composition_cache_lock:
        api_storage._composition_cache.clear()
        api_storage._composition_inflight.clear()


def test_resolve_endpoints_by_direction():
    pull = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "pull"}
    push = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "push"}
    bisync = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "bisync"}
    assert api_storage._resolve_endpoints(pull) == ("gd:Backup", "/mnt/data")
    assert api_storage._resolve_endpoints(push) == ("/mnt/data", "gd:Backup")
    assert api_storage._resolve_endpoints(bisync) == ("/mnt/data", "gd:Backup")


def test_blank_size_path_uses_the_same_response_contract():
    assert api_storage._rclone_size("") == {"path": "", "error": "Pfad fehlt"}


class _FakeListingProcess:
    def __init__(self, output: str, returncode: int = 0, error: str = ""):
        self.stdout = StringIO(output)
        self.stderr = StringIO(error)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_composition_aggregates_types_without_returning_file_names(monkeypatch):
    commands = []

    def popen(command, **_kwargs):
        commands.append(command)
        return _FakeListingProcess(
            '2048,DCIM/photo.jpg\n1024,DCIM/clip.mov\n512,"docs/report, final.pdf"\n32,README\n'
        )

    monkeypatch.setattr(api_storage.subprocess, "Popen", popen)

    result = api_storage._rclone_composition(
        "pcloud:/Fotos", filter_args=("--exclude", "/.work/**")
    )

    assert result["count"] == 4
    assert result["bytes"] == 3616
    assert {item["label"] for item in result["categories"]} == {
        "Bilder",
        "Videos",
        "Dokumente",
        "Ohne Endung",
    }
    assert {item["label"] for item in result["extensions"]} == {
        "JPG",
        "MOV",
        "PDF",
        "Ohne Endung",
    }
    assert "photo.jpg" not in str(result)
    assert commands[0][-4:] == ["--exclude", "/.work/**", "--", "pcloud:/Fotos"]


def test_composition_endpoint_resolves_side_and_reuses_cache(monkeypatch):
    pair = {
        "id": "recipes",
        "name": "Rezepte",
        "local": "/mnt/data/rezepte",
        "remote": "pcloud:/Rezepte",
        "direction": "push",
    }
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    calls = []

    def measure(path, **kwargs):
        calls.append((path, kwargs))
        return {
            "path": path,
            "count": 2,
            "bytes": 42,
            "truncated": False,
            "categories": [],
            "extensions": [],
        }

    monkeypatch.setattr(api_storage, "_rclone_composition", measure)

    first = api_storage.composition(pair="recipes", side="target")
    cached = api_storage.composition(pair="Rezepte", side="target")
    refreshed = api_storage.composition(pair="recipes", side="target", refresh=True)

    assert first["path"] == "pcloud:/Rezepte"
    assert first["side"] == "target"
    assert first["status"] == "fresh"
    assert cached["status"] == "cached"
    assert refreshed["status"] == "fresh"
    assert len(calls) == 2


def test_composition_keeps_partial_aggregates_but_does_not_cache_them(monkeypatch):
    pair = {"id": "photos", "name": "Fotos", "direction": "push"}
    calls = 0

    def measure(_path, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "path": "/mnt/photos",
            "count": 3,
            "bytes": 99,
            "categories": [
                {"key": "images", "label": "Bilder", "count": 3, "bytes": 99}
            ],
            "extensions": [],
            "truncated": False,
            "error": "Dateityp-Analyse fehlgeschlagen (rclone exit=6)",
        }

    monkeypatch.setattr(api_storage, "_rclone_composition", measure)

    first = api_storage._cached_composition(pair, "source", "/mnt/photos")
    second = api_storage._cached_composition(pair, "source", "/mnt/photos")

    assert first["status"] == "partial"
    assert first["categories"][0]["label"] == "Bilder"
    assert second["status"] == "partial"
    assert calls == 2
    assert not api_storage._composition_cache


def test_rclone_size_preserves_parseable_values_after_traversal_error(monkeypatch):
    def run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=6,
            stdout='{"count":214,"bytes":783674923,"sizeless":0}\n',
            stderr=(
                'ERROR : backups: failed to open directory "backups": permission denied'
            ),
        )

    monkeypatch.setattr(api_storage.subprocess, "run", run)

    result = api_storage._rclone_size("/mnt/data/rezepte")

    assert result["count"] == 214
    assert result["bytes"] == 783_674_923
    assert "permission denied" in result["error"]


def test_cached_size_exposes_partial_values_without_caching_them_as_complete(
    monkeypatch,
):
    pair = {
        "id": "recipes",
        "name": "Rezepte",
        "local": "/mnt/data/rezepte",
        "remote": "pcloud:/Rezepte",
        "direction": "push",
    }
    monkeypatch.setattr(
        api_storage,
        "_rclone_size",
        lambda _path: {
            "path": "/mnt/data/rezepte",
            "count": 214,
            "bytes": 783_674_923,
            "error": "permission denied",
        },
    )

    result = api_storage._cached_rclone_size(pair, "source", "/mnt/data/rezepte")

    assert result["count"] == 214
    assert result["bytes"] == 783_674_923
    assert result["measurement_status"] == "partial"
    assert result["measurement_state"] == "failed"
    assert result["measurement_error"] == "permission denied"
    assert not api_storage._size_cache


def test_overview_measures_with_the_same_pair_filters_as_sync(monkeypatch):
    pair = {
        "id": "recipes",
        "name": "Rezepte",
        "local": "/mnt/data/rezepte",
        "remote": "pcloud:/Rezepte",
        "direction": "push",
        "exclude": "/.work/**",
    }
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)
    calls = []

    def measure(path, **kwargs):
        calls.append((path, kwargs))
        return {"path": path, "count": 1, "bytes": 2}

    monkeypatch.setattr(api_storage, "_rclone_size", measure)

    api_storage.overview(include_remote=True, refresh_sizes=True)

    assert len(calls) == 2
    assert all(call[1]["filter_args"] == ("--exclude", "/.work/**") for call in calls)


class _FakeConfig:
    def __init__(self, pairs):
        self._pairs = pairs

    def get(self, *keys, default=None):
        if keys == ("backup", "pairs"):
            return self._pairs
        return default


class _FakeDB:
    def __init__(self, histories=None):
        self.histories = histories or {}
        self.identities = None

    def pair_last_history(self, identities):
        self.identities = identities
        return self.histories


def test_overview_without_sizes_skips_rclone(monkeypatch):
    pairs = [{"name": "A", "local": "/mnt/a", "remote": "gd:a", "direction": "pull"}]
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(
        api_storage, "_disk_usage", lambda p: {"path": p, "exists": True}
    )

    def _boom(*a, **k):
        raise AssertionError("rclone size darf ohne include_remote nicht laufen")

    monkeypatch.setattr(api_storage, "_rclone_size", _boom)
    result = api_storage.overview(include_remote=False)
    item = result["pairs"][0]
    assert item["source"] == "gd:a" and item["target"] == "/mnt/a"
    assert "source_size" not in item and "target_size" not in item


def test_overview_with_sizes_populates_both_sides(monkeypatch):
    pairs = [{"name": "A", "local": "/mnt/a", "remote": "gd:a", "direction": "push"}]
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda p: None)

    sizes = {
        "/mnt/a": {"count": 12, "bytes": 2048},
        "gd:a": {"count": 9, "bytes": 1024},
    }
    monkeypatch.setattr(
        api_storage, "_rclone_size", lambda path, **k: {"path": path, **sizes[path]}
    )

    result = api_storage.overview(include_remote=True)
    item = result["pairs"][0]
    # push: source=local, target=remote
    assert item["source_size"]["count"] == 12
    assert item["source_size"]["bytes"] == 2048
    assert item["target_size"]["count"] == 9
    assert item["target_size"]["bytes"] == 1024
    assert item["source_size"]["measurement_status"] == "fresh"
    assert item["source_size"]["measured_at"] is not None


def test_size_results_are_reused_within_ttl_and_can_be_refreshed(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)
    clock = [1_000.0]
    monkeypatch.setattr(api_storage.time, "time", lambda: clock[0])
    calls: list[str] = []

    def measure(path, **_kwargs):
        calls.append(path)
        return {"path": path, "count": len(calls), "bytes": 100 + len(calls)}

    monkeypatch.setattr(api_storage, "_rclone_size", measure)

    first = api_storage.overview(include_remote=True)["pairs"][0]
    clock[0] += 10
    cached = api_storage.overview(include_remote=True)["pairs"][0]
    clock[0] += 10
    refreshed = api_storage.overview(include_remote=True, refresh_sizes=True)["pairs"][
        0
    ]

    assert len(calls) == 4
    assert first["source_size"]["measurement_status"] == "fresh"
    assert cached["source_size"]["measurement_status"] == "cached"
    assert cached["source_size"]["measured_at"] == 1_000.0
    assert refreshed["source_size"]["measurement_status"] == "fresh"
    assert refreshed["source_size"]["measured_at"] == 1_020.0


def test_size_cache_is_bound_to_stable_pair_side_direction_and_path(monkeypatch):
    base = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    calls: list[str] = []
    monkeypatch.setattr(
        api_storage,
        "_rclone_size",
        lambda path: calls.append(path) or {"path": path, "count": 1, "bytes": 2},
    )

    first = api_storage._cached_rclone_size(base, "source", "/mnt/photos")
    renamed = api_storage._cached_rclone_size(
        {**base, "name": "Fotos neu"}, "source", "/mnt/photos"
    )
    reversed_direction = api_storage._cached_rclone_size(
        {**base, "direction": "pull"}, "source", "/mnt/photos"
    )
    other_path = api_storage._cached_rclone_size(base, "source", "/mnt/photos-neu")

    assert first["measurement_status"] == "fresh"
    assert renamed["measurement_status"] == "cached"
    assert reversed_direction["measurement_status"] == "fresh"
    assert other_path["measurement_status"] == "fresh"
    assert calls == ["/mnt/photos", "/mnt/photos", "/mnt/photos-neu"]


def test_concurrent_cold_cache_measurements_share_one_result(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "direction": "push",
    }
    callers = 12
    ready = threading.Barrier(callers)
    measurement_started = threading.Event()
    release_measurement = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def measure(path):
        nonlocal call_count
        with call_lock:
            call_count += 1
        measurement_started.set()
        assert release_measurement.wait(timeout=2)
        return {"path": path, "count": 7, "bytes": 8}

    def request_size():
        ready.wait(timeout=2)
        return api_storage._cached_rclone_size(pair, "source", "/mnt/photos")

    monkeypatch.setattr(api_storage, "_rclone_size", measure)
    with ThreadPoolExecutor(max_workers=callers) as executor:
        futures = [executor.submit(request_size) for _ in range(callers)]
        assert measurement_started.wait(timeout=2)
        time.sleep(0.05)
        release_measurement.set()
        results = [future.result(timeout=2) for future in futures]

    assert call_count == 1
    assert all(result == results[0] for result in results)
    assert results[0]["measurement_status"] == "fresh"
    assert not api_storage._size_inflight


def test_singleflight_propagates_same_measurement_exception(monkeypatch):
    pair = {"id": "photos", "name": "Fotos", "direction": "push"}
    callers = 6
    ready = threading.Barrier(callers)
    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def measure(_path):
        nonlocal call_count
        call_count += 1
        started.set()
        assert release.wait(timeout=2)
        raise RuntimeError("measurement exploded")

    def request_size():
        ready.wait(timeout=2)
        return api_storage._cached_rclone_size(pair, "source", "/mnt/photos")

    monkeypatch.setattr(api_storage, "_rclone_size", measure)
    with ThreadPoolExecutor(max_workers=callers) as executor:
        futures = [executor.submit(request_size) for _ in range(callers)]
        assert started.wait(timeout=2)
        time.sleep(0.05)
        release.set()
        errors = []
        for future in futures:
            with pytest.raises(RuntimeError, match="measurement exploded") as exc:
                future.result(timeout=2)
            errors.append(exc.value)

    assert call_count == 1
    assert all(error is errors[0] for error in errors)
    assert not api_storage._size_inflight


def test_different_measurement_keys_run_in_parallel(monkeypatch):
    barrier = threading.Barrier(2)
    calls: list[str] = []
    lock = threading.Lock()

    def measure(path):
        with lock:
            calls.append(path)
        barrier.wait(timeout=2)
        return {"path": path, "count": 1, "bytes": 2}

    monkeypatch.setattr(api_storage, "_rclone_size", measure)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            api_storage._cached_rclone_size,
            {"id": "a", "name": "A", "direction": "push"},
            "source",
            "/mnt/a",
        )
        second = executor.submit(
            api_storage._cached_rclone_size,
            {"id": "b", "name": "B", "direction": "push"},
            "source",
            "/mnt/b",
        )
        assert first.result(timeout=2)["count"] == 1
        assert second.result(timeout=2)["count"] == 1

    assert sorted(calls) == ["/mnt/a", "/mnt/b"]


def test_failed_measurement_is_not_cached_as_fresh_zero(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    results = iter(
        [
            {"path": "/mnt/photos", "count": 4, "bytes": 512},
            {"path": "/mnt/photos", "error": "Timeout"},
            {"path": "/mnt/photos", "error": "Timeout"},
        ]
    )
    monkeypatch.setattr(api_storage, "_rclone_size", lambda _path: next(results))

    fresh = api_storage._cached_rclone_size(pair, "source", "/mnt/photos")
    stale = api_storage._cached_rclone_size(
        pair, "source", "/mnt/photos", force_refresh=True
    )
    failed = api_storage._cached_rclone_size(
        {**pair, "id": "other"}, "source", "/mnt/photos"
    )

    assert fresh["measurement_status"] == "fresh"
    assert stale["measurement_status"] == "stale"
    assert stale["measurement_state"] == "stale"
    assert stale["count"] == 4 and stale["bytes"] == 512
    assert stale["measurement_error"] == "Timeout"
    assert failed["measurement_status"] == "failed"
    assert failed["measurement_state"] == "failed"
    assert failed["measured_at"] is None
    assert failed["error"] == "Timeout"
    assert "count" not in failed and "bytes" not in failed


def test_overview_keeps_last_sync_across_pair_rename(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos neu",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    key = "rclone:id:photos"
    database = _FakeDB(
        {
            key: {
                "last_result": None,
                "last_success": {
                    "ended_at": 1234,
                    "pair": {"name": "Fotos alt", "transferred": "2 GiB"},
                },
            }
        }
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    item = api_storage.overview()["pairs"][0]

    assert database.identities == {
        key: "Fotos neu",
        "restore:Fotos neu": "Fotos neu",
    }
    assert item["last_sync"] == 1234
    assert item["last_transferred"] == "2 GiB"


def test_overview_exposes_typed_restore_proof_without_mixing_sync_history(
    monkeypatch,
):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    database = _FakeDB(
        {
            "rclone:id:photos": {
                "last_result": None,
                "last_success": {"ended_at": 1200, "pair": {"transferred": "2 GiB"}},
            },
            "restore:Fotos": {
                "last_result": {
                    "ok": True,
                    "ended_at": 1300,
                    "job_id": 44,
                    "pair": {
                        "verified": 20,
                        "sample_size": 20,
                        "sample_status": "complete",
                    },
                },
                "last_success": {
                    "ok": True,
                    "ended_at": 1300,
                    "job_id": 44,
                    "pair": {
                        "verified": 20,
                        "sample_size": 20,
                        "sample_status": "complete",
                    },
                },
            },
        }
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    item = api_storage.overview()["pairs"][0]

    assert item["last_sync"] == 1200
    assert item["restore_evidence"] == {
        "state": "passed",
        "last_attempt_at": 1300,
        "last_success_at": 1300,
        "job_id": 44,
        "verified_files": 20,
        "sample_size": 20,
        "checksum_verified": True,
        "error": None,
    }


def test_overview_marks_failed_restore_but_keeps_previous_proof(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    database = _FakeDB(
        {
            "restore:Fotos": {
                "last_result": {
                    "ok": False,
                    "ended_at": 1400,
                    "job_id": 45,
                    "pair": {"error": "Prüfsummen weichen ab"},
                },
                "last_success": {
                    "ok": True,
                    "ended_at": 1300,
                    "job_id": 44,
                    "pair": {"verified": 20, "sample_size": 20},
                },
            }
        }
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    evidence = api_storage.overview()["pairs"][0]["restore_evidence"]

    assert evidence["state"] == "failed"
    assert evidence["last_attempt_at"] == 1400
    assert evidence["last_success_at"] == 1300
    assert evidence["verified_files"] == 20
    assert evidence["error"] == "Prüfsummen weichen ab"


def test_overview_normalizes_numeric_transfer_from_historic_run(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    key = "rclone:id:photos"
    database = _FakeDB(
        {
            key: {
                "last_result": None,
                "last_success": {
                    "ended_at": 1234,
                    "pair": {"transferred": 2048},
                },
            }
        }
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    item = api_storage.overview()["pairs"][0]

    assert item["last_transferred"] == "2048"


def test_overview_omits_missing_transfer_instead_of_sending_numeric_zero(monkeypatch):
    pair = {
        "id": "photos",
        "name": "Fotos",
        "local": "/mnt/photos",
        "remote": "gd:photos",
        "direction": "push",
    }
    key = "rclone:id:photos"
    database = _FakeDB(
        {
            key: {
                "last_result": None,
                "last_success": {"ended_at": 1234, "pair": {}},
            }
        }
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    item = api_storage.overview()["pairs"][0]

    assert "last_transferred" not in item


def test_overview_does_not_reuse_history_for_new_identity_or_restore_drill(
    monkeypatch,
):
    pair = {
        "id": "new-photos",
        "name": "Fotos",
        "local": "/mnt/new-photos",
        "remote": "gd:new-photos",
        "direction": "push",
    }
    # pair_last_history hat die typed-key-/Legacy-Fallback-Regeln bereits
    # angewendet: Ein alter Stable-Key und restoretest:pair:* sind keine
    # Kandidaten für die neue rclone-Identität.
    database = _FakeDB(
        {"rclone:id:new-photos": {"last_result": None, "last_success": None}}
    )
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig([pair]))
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    item = api_storage.overview()["pairs"][0]

    assert "last_sync" not in item
    assert "last_transferred" not in item


def _storage_pairs(count: int) -> list[dict]:
    return [
        {
            "id": f"pair-{index}",
            "name": f"Pair {index}",
            "local": f"/mnt/{index}",
            "remote": f"cloud:{index}",
            "direction": "push",
        }
        for index in range(count)
    ]


def test_four_pairs_measure_all_eight_sides_in_one_parallel_wave(monkeypatch):
    """Vier Pairs dürfen nicht mehr in zwei seriellen 45-s-Wellen laufen."""
    import threading

    pairs = _storage_pairs(4)
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)
    monkeypatch.setattr(api_storage, "_SIZE_MEASUREMENT_WORKERS", 8)
    barrier = threading.Barrier(8)
    active = 0
    peak = 0
    lock = threading.Lock()

    def measure(path, *, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=1)
            return {"path": path, "count": 1, "bytes": 2}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(api_storage, "_rclone_size", measure)

    result = api_storage.overview(include_remote=True, refresh_sizes=True)

    assert peak == 8
    assert active == 0
    assert result["measurement"] == {
        "state": "loaded",
        "total": 8,
        "loaded": 8,
        "failed": 0,
        "stale": 0,
        "measurement_error": None,
        "measured_at": result["measurement"]["measured_at"],
    }


def test_global_deadline_finishes_all_workers_and_marks_total_failure(monkeypatch):
    """Auch viele langsame Pfade verlassen keine Threads nach der API-Antwort."""
    import threading
    import time

    pairs = _storage_pairs(5)
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)
    monkeypatch.setattr(api_storage, "_SIZE_MEASUREMENT_WORKERS", 2)
    monkeypatch.setattr(api_storage, "_SIZE_MEASUREMENT_DEADLINE_SECONDS", 0.08)
    active = 0
    lock = threading.Lock()

    def measure(path, *, timeout):
        nonlocal active
        with lock:
            active += 1
        try:
            time.sleep(min(timeout, 0.1))
            return {"path": path, "error": "Timeout"}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(api_storage, "_rclone_size", measure)
    started = time.monotonic()

    result = api_storage.overview(include_remote=True, refresh_sizes=True)

    assert time.monotonic() - started < 0.3
    assert active == 0
    assert result["measurement"]["state"] == "failed"
    assert result["measurement"]["failed"] == 10
    assert all(
        item[f"{side}_measurement"]["state"] == "failed"
        for item in result["pairs"]
        for side in ("source", "target")
    )


def test_partial_measurement_is_machine_readable_per_side(monkeypatch):
    pairs = _storage_pairs(2)
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    def measure(path, **_kwargs):
        if path == "cloud:1":
            return {"path": path, "error": "Remote nicht erreichbar"}
        return {"path": path, "count": 3, "bytes": 4}

    monkeypatch.setattr(api_storage, "_rclone_size", measure)

    result = api_storage.overview(include_remote=True, refresh_sizes=True)

    assert result["measurement"]["state"] == "partial"
    assert result["measurement"]["loaded"] == 3
    assert result["measurement"]["failed"] == 1
    failed = result["pairs"][1]
    assert failed["source_measurement"]["state"] == "loaded"
    assert failed["target_measurement"] == {
        "path": "cloud:1",
        "state": "failed",
        "measurement_error": "Remote nicht erreichbar",
        "measured_at": None,
    }
    assert failed["target_size"]["measurement_status"] == "failed"
    assert failed["target_size"]["measurement_error"] == "Remote nicht erreichbar"


def test_base_overview_exposes_loading_metadata_without_overwriting_size_fields(
    monkeypatch,
):
    pairs = _storage_pairs(1)
    monkeypatch.setattr(api_storage, "get_config", lambda: _FakeConfig(pairs))
    monkeypatch.setattr(api_storage, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(api_storage, "_disk_usage", lambda _path: None)

    result = api_storage.overview(include_remote=False)
    pair = result["pairs"][0]

    assert "source_size" not in pair and "target_size" not in pair
    assert pair["source_measurement"]["state"] == "loading"
    assert pair["target_measurement"]["state"] == "loading"
    assert result["measurement"]["state"] == "loading"
