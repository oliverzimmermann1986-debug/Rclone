from pathlib import Path

from app.jobs import rclone_sync


class FakeConfig:
    def __init__(self, data):
        self.data = data

    def get(self, *keys, default=None):
        value = self.data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


def test_conflict_flags_only_apply_to_bisync():
    cfg = FakeConfig(
        {
            "backup": {
                "conflict_resolve": "newer",
                "recover": True,
                "resilient": True,
                "max_lock": "2m",
            }
        }
    )
    assert rclone_sync._global_verb_args(cfg, "copy") == []
    args = rclone_sync._global_verb_args(cfg, "bisync")
    assert "--conflict-resolve" in args
    assert "--recover" in args
    assert "--resilient" in args
    assert "--max-lock" in args


def test_backup_dir_follows_destination_for_one_way():
    cfg = FakeConfig({"backup": {"backup_dir": ".trash/{date}"}})
    args = rclone_sync._backup_dir_args(
        cfg,
        {},
        "copy",
        "cloud:/source",
        "/mnt/target",
    )
    assert args[0] == "--backup-dir"
    assert args[1].startswith("/mnt/target/.trash/")


def test_bisync_relative_backup_dir_maps_to_both_sides():
    cfg = FakeConfig({"backup": {"backup_dir": ".trash/{date}"}})
    args = rclone_sync._backup_dir_args(
        cfg,
        {},
        "bisync",
        "cloud:/source",
        "/mnt/target",
    )
    assert args[0] == "--backup-dir1"
    assert args[1].startswith("cloud:/source/.trash/")
    assert args[2] == "--backup-dir2"
    assert args[3].startswith("/mnt/target/.trash/")


def test_overlapping_paths_are_detected(tmp_path: Path):
    parent = tmp_path / "data"
    child = parent / "nested"
    child.mkdir(parents=True)
    pairs = [
        {"local": str(parent), "remote": "cloud:/one"},
        {"local": str(child), "remote": "cloud:/two"},
    ]
    assert rclone_sync._has_overlapping_pairs(pairs) is True


def test_command_separates_flags_from_paths(monkeypatch):
    cfg = FakeConfig(
        {
            "backup": {
                "require_delete_confirmation": True,
                "require_max_delete_for_sync": True,
                "tuning": {"stats_interval": "10s"},
            }
        }
    )
    monkeypatch.setattr(rclone_sync, "get_config", lambda: cfg)
    pair = {
        "name": "safe",
        "remote": "cloud:/source",
        "local": "/mnt/target",
        "direction": "pull",
        "mode": "copy",
    }
    cmd, *_ = rclone_sync._build_pair_command(
        pair, ["--exclude", "Folder with spaces/**"], True
    )
    separator = cmd.index("--")
    assert cmd[separator + 1 :] == ["cloud:/source", "/mnt/target"]
    assert cmd[cmd.index("--exclude") + 1] == "Folder with spaces/**"
    assert "--dry-run" in cmd[:separator]


def test_production_sync_requires_delete_confirmation(monkeypatch):
    cfg = FakeConfig(
        {
            "backup": {
                "require_delete_confirmation": True,
                "require_max_delete_for_sync": True,
                "tuning": {"stats_interval": "10s"},
            }
        }
    )
    monkeypatch.setattr(rclone_sync, "get_config", lambda: cfg)
    pair = {
        "name": "mirror",
        "remote": "cloud:/source",
        "local": "/mnt/target",
        "direction": "pull",
        "mode": "sync",
        "max_delete": 100,
    }
    import pytest

    with pytest.raises(ValueError, match="allow_delete"):
        rclone_sync._build_pair_command(pair, [], False)
    pair["allow_delete"] = True
    cmd, verb, *_ = rclone_sync._build_pair_command(pair, [], False)
    assert verb == "sync"
    assert "--max-delete=100" in cmd


def test_production_bisync_requires_delete_confirmation(monkeypatch):
    import pytest

    cfg = FakeConfig(
        {
            "backup": {
                "require_delete_confirmation": True,
                "require_max_delete_for_sync": True,
                "tuning": {"stats_interval": "10s", "max_delete": 100},
            }
        }
    )
    monkeypatch.setattr(rclone_sync, "get_config", lambda: cfg)
    pair = {
        "name": "two-way",
        "remote": "cloud:/source",
        "local": "/mnt/target",
        "direction": "bisync",
        "mode": "bisync",
    }
    with pytest.raises(ValueError, match="allow_delete"):
        rclone_sync._build_pair_command(pair, [], False)

    pair["allow_delete"] = True
    cmd, verb, *_ = rclone_sync._build_pair_command(pair, [], False)
    assert verb == "bisync"
    assert "--max-delete=100" in cmd
