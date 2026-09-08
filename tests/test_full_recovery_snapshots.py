from pathlib import Path
import io
import json

import pytest

from app.db import Database
from app import recovery_snapshots as snapshots
from app.recovery_points import compare_points, list_points, point_target


def fixture(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    pair = {
        "id": "documents",
        "name": "Dokumente",
        "direction": "push",
        "mode": "copy",
        "local": str(source),
        "remote": str(target),
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "server"),
            "recovery_dir": str(tmp_path / "staging"),
        },
        "backup": {"pairs": [pair]},
    }
    return config, pair, target


def test_full_point_preserves_unchanged_deleted_and_changed_files(tmp_path):
    config, pair, target = fixture(tmp_path)
    (target / "empty").mkdir()
    (target / "unchanged.txt").write_bytes(b"same")
    (target / "deleted.txt").write_bytes(b"deleted later")
    (target / "changed.txt").write_bytes(b"old")
    old = snapshots.capture_snapshot(config, pair, max_total_mb=1)
    (target / "deleted.txt").unlink()
    (target / "changed.txt").write_bytes(b"new")
    (target / "added.txt").write_bytes(b"new file")
    new = snapshots.capture_snapshot(config, pair, max_total_mb=1)
    assert old["complete"] and old["files"] == 3
    assert new["id"] != old["id"]
    assert len(list_points(config, pair)) == 3
    comparison = compare_points(config, pair, old["id"], new["id"])
    assert comparison["counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert comparison["changed"][0]["path"] == "changed.txt"
    assert comparison["verification"] == "sha256"
    database = Database(tmp_path / "state.db")
    job_id = database.job_start("recovery")
    result = snapshots.run_snapshot_restore(
        database, config, pair, point_id=old["id"], max_total_mb=1, job_id=job_id
    )
    assert result["verified"] and result["files"] == 3
    restored = Path(result["staging_path"])
    assert (restored / "unchanged.txt").read_bytes() == b"same"
    assert (restored / "deleted.txt").read_bytes() == b"deleted later"
    assert (restored / "changed.txt").read_bytes() == b"old"
    assert not (restored / "added.txt").exists()
    assert (restored / "empty").is_dir()
    assert (target / "changed.txt").read_bytes() == b"new"
    assert not (target / "deleted.txt").exists()


def test_same_size_corruption_fails_hash_and_removes_partial_restore(tmp_path):
    config, pair, target = fixture(tmp_path)
    (target / "a").write_bytes(b"abc")
    point = snapshots.capture_snapshot(config, pair)
    (Path(point_target(config, pair, point["id"])) / "a").write_bytes(b"xyz")
    database = Database(tmp_path / "state.db")
    job_id = database.job_start("recovery")
    result = snapshots.run_snapshot_restore(
        database, config, pair, point_id=point["id"], max_total_mb=1, job_id=job_id
    )
    assert result["status"] == "error"
    assert result["verified"] is False
    assert not (Path(config["paths"]["recovery_dir"]) / result["id"]).exists()


def test_snapshot_rejects_target_rebinding_and_oversize(tmp_path):
    config, pair, target = fixture(tmp_path)
    (target / "a").write_bytes(b"abc")
    point = snapshots.capture_snapshot(config, pair)
    changed = {**pair, "remote": str(tmp_path / "other")}
    with pytest.raises(snapshots.SnapshotError, match="gehört"):
        snapshots.load_manifest(config, changed, point["id"])
    (target / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(snapshots.SnapshotError, match="Größenlimit"):
        snapshots.capture_snapshot(config, pair, max_total_mb=1)
    assert not list(snapshots.snapshot_root(config).glob(".capture-*"))


def test_snapshot_never_lives_inside_sync_root(tmp_path):
    config, pair, target = fixture(tmp_path)
    config["paths"]["recovery_snapshots_dir"] = str(target / "snapshots")
    with pytest.raises(snapshots.SnapshotError, match="außerhalb"):
        snapshots.capture_snapshot(config, pair)
    assert not (target / "snapshots").exists()


def test_full_restore_never_stages_inside_a_live_data_path(tmp_path):
    config, pair, target = fixture(tmp_path)
    (target / "original").write_bytes(b"untouched")
    point = snapshots.capture_snapshot(config, pair)
    config["paths"]["recovery_dir"] = str(target / "unsafe-staging")
    database = Database(tmp_path / "state.db")
    job_id = database.job_start("recovery")
    result = snapshots.run_snapshot_restore(
        database, config, pair, point_id=point["id"], max_total_mb=1, job_id=job_id
    )
    assert result["status"] == "error" and not result["verified"]
    assert not (target / "unsafe-staging").exists()
    assert (target / "original").read_bytes() == b"untouched"


def test_post_sync_capture_requires_explicit_opt_in(tmp_path):
    config, pair, target = fixture(tmp_path)
    (target / "a").write_bytes(b"proof")
    assert snapshots.capture_if_enabled(config, pair) is None
    enabled = {**pair, "recovery_snapshots": True}
    assert snapshots.capture_if_enabled(config, enabled)["complete"] is True


def test_remote_snapshot_checks_all_bytes_and_labels_unknown_live_hashes(
    tmp_path, monkeypatch
):
    config, pair, _target = fixture(tmp_path)
    pair["remote"] = "cloud:/Documents"
    listing = [
        {
            "Path": "proof.txt",
            "Size": 5,
            "IsDir": False,
            "ModTime": "2026-09-08T12:00:00Z",
        }
    ]

    class Process:
        def __init__(self, command, **kwargs):
            assert command[:3] == ["rclone", "lsjson", "--recursive"]
            self.stdout = io.BytesIO(json.dumps(listing).encode())
            self.returncode = None

        def wait(self, timeout):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

    commands = []

    def transfer(command, **kwargs):
        commands.append(command)
        if command[1] == "copy":
            (Path(command[-1]) / "proof.txt").write_bytes(b"proof")

    monkeypatch.setattr(snapshots.subprocess, "Popen", Process)
    monkeypatch.setattr(snapshots, "_run", transfer)

    point = snapshots.capture_snapshot(config, pair, max_total_mb=1)

    assert point["complete"] and point["files"] == 1
    assert commands[-1][:3] == ["rclone", "check", "--download"]
    comparison = compare_points(config, pair, point["id"], "current")
    assert comparison["truncated"] is True
    assert comparison["unverified"] == [{"path": "proof.txt", "size": 5}]
    assert comparison["verification"] == "metadata"


def test_oversize_remote_inventory_is_killed_before_publication(tmp_path, monkeypatch):
    config, pair, _target = fixture(tmp_path)
    pair["remote"] = "cloud:/Documents"
    processes = []

    class Process:
        def __init__(self, command, **kwargs):
            self.stdout = io.BytesIO(b"x" * 1024)
            self.returncode = None
            self.killed = False
            processes.append(self)

        def wait(self, timeout):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -1

    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", 100)
    monkeypatch.setattr(snapshots.subprocess, "Popen", Process)

    with pytest.raises(snapshots.SnapshotError, match="Manifestlimit"):
        snapshots.capture_snapshot(config, pair, max_total_mb=1)

    assert processes[0].killed
    assert list(snapshots.snapshot_root(config).iterdir()) == []
