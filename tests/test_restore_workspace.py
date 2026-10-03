from __future__ import annotations

import json
from pathlib import Path

from app.jobs import restore_workspace as workspace


def test_restart_removes_owned_scratch_but_preserves_live_workspace(tmp_path: Path):
    orphan, orphan_lease = workspace.create_workspace(tmp_path, "restore-orphan-")
    live, live_lease = workspace.create_workspace(tmp_path, "restore-live-")
    (orphan / "photo.jpg").write_bytes(b"downloaded private data")
    (live / "photo.jpg").write_bytes(b"still in use")
    orphan_lease.release()  # OS releases this lease on a hard process exit.
    try:
        assert workspace.cleanup_orphaned_workspaces(tmp_path) == {
            "removed": 1,
            "failed": 0,
        }
        assert not orphan.exists()
        assert (live / "photo.jpg").read_bytes() == b"still in use"
    finally:
        live_lease.release()
    assert workspace.cleanup_orphaned_workspaces(tmp_path)["removed"] == 1


def test_cleanup_preserves_unmarked_and_mismatched_directories(tmp_path: Path):
    for name, marker in (
        ("restore-legacy", None),
        (
            "restore-unrelated",
            {"version": 1, "kind": "other", "name": "restore-unrelated"},
        ),
        (
            "restore-mismatch",
            {"version": 1, "kind": "restore-drill", "name": "another"},
        ),
    ):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "keep").write_bytes(b"preserve")
        if marker is not None:
            (tmp_path / workspace.REGISTRY).mkdir(exist_ok=True)
            workspace._record_path(directory).write_text(json.dumps(marker))
    assert workspace.cleanup_orphaned_workspaces(tmp_path) == {
        "removed": 0,
        "failed": 0,
    }
    assert len(list(tmp_path.glob("restore-*/keep"))) == 3


def test_failed_cleanup_retains_marker_for_next_restart(tmp_path: Path, monkeypatch):
    directory, lease = workspace.create_workspace(tmp_path, "restore-retry-")
    lease.release()
    remove = workspace.shutil.rmtree

    def fail(_path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(workspace.shutil, "rmtree", fail)
    assert workspace.cleanup_orphaned_workspaces(tmp_path) == {
        "removed": 0,
        "failed": 1,
    }
    assert workspace._record_path(directory).is_file()
    monkeypatch.setattr(workspace.shutil, "rmtree", remove)
    assert workspace.cleanup_orphaned_workspaces(tmp_path)["removed"] == 1


def test_partial_cleanup_keeps_external_receipt_until_all_data_are_removed(
    tmp_path: Path, monkeypatch
):
    directory, lease = workspace.create_workspace(tmp_path, "restore-partial-")
    lease.release()
    (directory / "first").write_bytes(b"first")
    (directory / "private-photo").write_bytes(b"private")
    remove = workspace.shutil.rmtree

    def partial_failure(path):
        (path / "first").unlink()
        raise OSError("second file cannot be deleted yet")

    monkeypatch.setattr(workspace.shutil, "rmtree", partial_failure)
    assert workspace.cleanup_orphaned_workspaces(tmp_path)["failed"] == 1
    assert workspace._record_path(directory).is_file()
    assert (directory / "private-photo").read_bytes() == b"private"
    monkeypatch.setattr(workspace.shutil, "rmtree", remove)
    assert workspace.cleanup_orphaned_workspaces(tmp_path)["removed"] == 1
    assert not workspace._record_path(directory).exists()


def test_cleanup_never_follows_directory_or_marker_symlinks(tmp_path: Path):
    import pytest

    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "keep").write_bytes(b"preserve")
    linked = tmp_path / "restore-link"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    directory = tmp_path / "restore-marker-link"
    directory.mkdir()
    (tmp_path / workspace.REGISTRY).mkdir(exist_ok=True)
    source = tmp_path / "marker.json"
    source.write_text(
        json.dumps({"version": 1, "kind": "restore-drill", "name": directory.name})
    )
    workspace._record_path(directory).symlink_to(source)
    workspace._record_path(linked).write_text(
        json.dumps({"version": 1, "kind": "restore-drill", "name": linked.name})
    )
    assert workspace.cleanup_orphaned_workspaces(tmp_path)["removed"] == 0
    assert (outside / "keep").read_bytes() == b"preserve"
    assert directory.is_dir()
