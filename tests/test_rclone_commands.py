from pathlib import Path

import pytest

from app.jobs import rclone_sync


def test_remote_precheck_uses_supported_lsjson_flags(monkeypatch):
    captured = []

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **_kwargs):
        captured.append(command)
        return Result()

    monkeypatch.setattr(rclone_sync.subprocess, "run", fake_run)

    assert rclone_sync._remote_reachable("pcloud:/folder") == (True, "ok")
    assert captured[0][:3] == ["rclone", "lsjson", "--stat"]
    assert "--no-size" not in captured[0]


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


def test_hidden_pair_options_are_not_consumed(monkeypatch):
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
        "name": "canonical",
        "remote": "cloud:/source",
        "local": "/mnt/target",
        "direction": "pull",
        "mode": "copy",
        "transfers": 3,
        "options": {"transfers": 99, "ignore_errors": True},
    }

    cmd, *_ = rclone_sync._build_pair_command(pair, [], True)

    assert "--transfers=3" in cmd
    assert "--transfers=99" not in cmd
    assert "--ignore-errors" not in cmd


def test_pre_spawn_recheck_blocks_process_creation(tmp_path: Path, monkeypatch):
    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("Popen darf nach fehlgeschlagenem Recheck nicht laufen")

    monkeypatch.setattr(rclone_sync.subprocess, "Popen", unexpected_popen)

    with pytest.raises(RuntimeError, match="Sicherheits-Recheck"):
        rclone_sync._run_rclone_command(
            ["rclone", "version"],
            tmp_path / "run.log",
            timeout_sec=1,
            pre_spawn_check=lambda: (False, "Pfad ausgetauscht"),
        )


def test_cancel_after_safety_recheck_still_blocks_process_creation(
    tmp_path: Path, monkeypatch
):
    cancelled = False

    def cancel_during_recheck():
        nonlocal cancelled
        cancelled = True
        return True, "ok"

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("Popen darf nach Cancel nicht laufen")

    monkeypatch.setattr(
        rclone_sync,
        "is_cancelled",
        lambda _scope=rclone_sync.DEFAULT_CANCEL_SCOPE: cancelled,
    )
    monkeypatch.setattr(rclone_sync.subprocess, "Popen", unexpected_popen)

    rc = rclone_sync._run_rclone_command(
        ["rclone", "version"],
        tmp_path / "run.log",
        timeout_sec=1,
        pre_spawn_check=cancel_during_recheck,
    )

    assert rc == 130


def test_local_source_identity_swap_is_detected(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    pair = {
        "remote": "cloud:/target",
        "local": str(source),
        "direction": "push",
        "min_local_files": 0,
    }
    guards = rclone_sync._capture_local_endpoint_guards(pair)
    source.rename(tmp_path / "old-source")
    source.mkdir()

    ok, message = rclone_sync._recheck_local_endpoint_guards(pair, guards)

    assert ok is False
    assert "geändert" in message


def test_local_to_local_remote_source_gets_file_guard(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    pair = {
        "remote": str(source),
        "local": str(target),
        "direction": "pull",
        "mode": "copy",
        "min_remote_files": 1,
        "min_local_files": 0,
    }

    ok, message = rclone_sync._precheck_pair(pair)

    assert ok is False
    assert "min_remote_files=1" in message


def test_local_to_local_push_can_guard_existing_remote_destination(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "payload.txt").write_text("data", encoding="utf-8")
    pair = {
        "remote": str(target),
        "local": str(source),
        "direction": "push",
        "min_local_files": 1,
        "min_remote_files": 1,
    }

    ok, message = rclone_sync._precheck_pair(pair)

    assert ok is False
    assert "min_remote_files=1" in message


def test_dynamic_scheduler_skips_currently_conflicting_pair():
    active = {
        "name": "active",
        "local": "/srv/a",
        "remote": "cloud:/one",
    }
    pending = [
        {"name": "blocked", "local": "/srv/a/child", "remote": "cloud:/two"},
        {"name": "ready", "local": "/srv/b", "remote": "cloud:/three"},
    ]

    assert rclone_sync._next_runnable_pair_index(pending, [active]) == 1
