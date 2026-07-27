"""Tests für Quelle/Ziel-Auflösung und Dateizahl/Größe in der Storage-Übersicht."""

from app.routes import api_storage


def test_resolve_endpoints_by_direction():
    pull = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "pull"}
    push = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "push"}
    bisync = {"local": "/mnt/data", "remote": "gd:Backup", "direction": "bisync"}
    assert api_storage._resolve_endpoints(pull) == ("gd:Backup", "/mnt/data")
    assert api_storage._resolve_endpoints(push) == ("/mnt/data", "gd:Backup")
    assert api_storage._resolve_endpoints(bisync) == ("/mnt/data", "gd:Backup")


class _FakeConfig:
    def __init__(self, pairs):
        self._pairs = pairs

    def get(self, *keys, default=None):
        if keys == ("backup", "pairs"):
            return self._pairs
        return default


class _FakeDB:
    def pair_last_successes(self):
        return {}


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
