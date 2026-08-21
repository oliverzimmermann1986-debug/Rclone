"""Tests für Quelle/Ziel-Auflösung und Dateizahl/Größe in der Storage-Übersicht."""

import pytest

from app.routes import api_storage


@pytest.fixture(autouse=True)
def _clear_size_cache():
    with api_storage._size_cache_lock:
        api_storage._size_cache.clear()
    yield
    with api_storage._size_cache_lock:
        api_storage._size_cache.clear()


def test_resolve_endpoints_by_direction():
    pull = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "pull"}
    push = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "push"}
    bisync = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "bisync"}
    assert api_storage._resolve_endpoints(pull) == ("gd:Backup", "/mnt/data")
    assert api_storage._resolve_endpoints(push) == ("/mnt/data", "gd:Backup")
    assert api_storage._resolve_endpoints(bisync) == ("/mnt/data", "gd:Backup")


def test_blank_size_path_uses_the_same_response_contract():
    assert api_storage._rclone_size("") == {"path": "", "error": "Pfad fehlt"}


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

    def measure(path):
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
    assert stale["count"] == 4 and stale["bytes"] == 512
    assert stale["measurement_error"] == "Timeout"
    assert failed["measurement_status"] == "failed"
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

    assert database.identities == {key: "Fotos neu"}
    assert item["last_sync"] == 1234
    assert item["last_transferred"] == "2 GiB"


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
