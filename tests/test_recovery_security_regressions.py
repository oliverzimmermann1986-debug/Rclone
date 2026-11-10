from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import recovery_points as points, recovery_snapshots as snapshots
from app.db import Database
from app.jobs import selective_restore
from app.recovery_paths import validated_staging_path
from app import recovery_walk


def recovery_config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    pair = {
        "id": "documents",
        "name": "Documents",
        "direction": "push",
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
    return config, pair


@pytest.mark.parametrize("overlap", ["source", "child", "parent", "target", "snapshot"])
def test_staging_overlap_is_rejected_before_directory_side_effects(tmp_path, overlap):
    config, pair = recovery_config(tmp_path)
    source = Path(pair["local"])
    candidate = {
        "source": source,
        "child": source / "staging",
        "parent": tmp_path,
        "target": Path(pair["remote"]),
        "snapshot": tmp_path / "server" / "recovery-snapshots" / "staging",
    }[overlap]
    config["paths"]["recovery_dir"] = str(candidate)
    existed = candidate.exists()
    original_mode = stat.S_IMODE(candidate.stat().st_mode) if existed else None
    with pytest.raises(ValueError, match="außerhalb"):
        selective_restore.staging_root(config)
    assert candidate.exists() == existed
    if existed:
        assert stat.S_IMODE(candidate.stat().st_mode) == original_mode


def test_selective_restore_checks_unconfigured_pair_before_touching_source(
    tmp_path, monkeypatch
):
    config, pair = recovery_config(tmp_path)
    config["backup"]["pairs"] = []
    config["paths"]["recovery_dir"] = pair["local"]
    source = Path(pair["local"])
    (source / "keep").write_bytes(b"original")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    database = Database(tmp_path / "state.db")
    job_id = database.job_start("recovery")
    runner = Mock(side_effect=AssertionError("overlap must prevent transfer"))
    monkeypatch.setattr(selective_restore, "_run", runner)
    monkeypatch.setattr(selective_restore, "notify", lambda *a, **k: None)
    result = selective_restore.run_selective_restore(
        database, config, pair, ["keep"], max_total_mb=1, job_id=job_id
    )
    assert result["status"] == "error"
    assert "außerhalb" in result["error"]
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    assert list(source.iterdir()) == [source / "keep"]
    runner.assert_not_called()


def test_staging_reads_do_not_create_root_and_reject_other_pair_overlap(tmp_path):
    config, pair = recovery_config(tmp_path)
    database = Database(tmp_path / "state.db")
    assert selective_restore.list_staging(database, config) == []
    assert not Path(config["paths"]["recovery_dir"]).exists()
    config["backup"]["pairs"].append({**pair, "local": str(tmp_path / "other")})
    config["paths"]["recovery_dir"] = str(tmp_path / "other" / "staging")
    with pytest.raises(ValueError, match="außerhalb"):
        validated_staging_path(config, pair=pair)
    with pytest.raises(ValueError, match="außerhalb"):
        selective_restore.list_staging(database, config)
    with pytest.raises(ValueError, match="außerhalb"):
        selective_restore.remove_staging(database, config, "recovery-1")
    assert not (tmp_path / "other").exists()


def test_overlimit_file_is_rejected_before_hashing(tmp_path, monkeypatch):
    (tmp_path / "large").write_bytes(b"0123456789")
    hashing = Mock(side_effect=AssertionError("must reject before reading content"))
    monkeypatch.setattr(snapshots, "_hash_inventory_file", hashing)
    with pytest.raises(snapshots.SnapshotError, match="Größenlimit"):
        snapshots._inventory_local(tmp_path, limit_bytes=5, hashes=True)
    hashing.assert_not_called()


def test_inventory_hash_rejects_changed_file_and_accepts_exact_bytes(tmp_path):
    path = tmp_path / "sample"
    path.write_bytes(b"sample")
    expected = path.stat()
    assert (
        snapshots._hash_inventory_file(path, expected)
        == hashlib.sha256(b"sample").hexdigest()
    )
    path.write_bytes(b"changed size")
    with pytest.raises(snapshots.SnapshotError, match="verändert"):
        snapshots._hash_inventory_file(path, expected)


@pytest.mark.parametrize("inventory_kind", ["snapshot", "comparison"])
def test_local_inventory_stops_reading_oversized_directory(
    tmp_path, monkeypatch, inventory_kind
):
    class HugeDirectory:
        scanned = 0
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def __iter__(self):
            for index in range(1000):
                self.scanned += 1
                yield SimpleNamespace(name=f"file-{index}", is_dir=lambda: False)

    entries = HugeDirectory()
    monkeypatch.setattr(recovery_walk.os, "scandir", lambda root: entries)
    if inventory_kind == "snapshot":
        monkeypatch.setattr(snapshots, "MAX_FILES", 3)
        with pytest.raises(snapshots.SnapshotError, match="Datei- oder Größenlimit"):
            snapshots._inventory_local(tmp_path, limit_bytes=1024)
    else:
        monkeypatch.setattr(points, "MAX_DIFF_SCAN", 3)
        assert points._local_inventory(str(tmp_path)) == ({}, True)
    assert entries.scanned == 4
    assert entries.closed


def test_local_inventory_preserves_empty_directories_and_file_limit(
    tmp_path, monkeypatch
):
    (tmp_path / "empty").mkdir()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "file").write_bytes(b"content")
    files, directories = snapshots._inventory_local(
        tmp_path, limit_bytes=7, hashes=True
    )
    assert directories == ["empty", "nested"]
    assert files["nested/file"]["size"] == 7
    monkeypatch.setattr(points, "MAX_DIFF_FILES", 1)
    compared, truncated = points._local_inventory(str(tmp_path))
    assert set(compared) == {"nested/file"} and truncated


def test_verified_copy_bounds_growing_source_before_writing_extra_bytes(
    tmp_path, monkeypatch
):
    origin, destination = tmp_path / "source", tmp_path / "copy"
    origin.write_bytes(b"abc")
    original_fdopen = snapshots.os.fdopen
    reads = []

    class GrowingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size):
            reads.append(size)
            if len(reads) == 1:
                with origin.open("ab") as writer:
                    writer.write(b"growth" * 1000)
            return self.handle.read(size)

    monkeypatch.setattr(
        snapshots.os,
        "fdopen",
        lambda *args, **kwargs: GrowingReader(original_fdopen(*args, **kwargs)),
    )
    with pytest.raises(snapshots.SnapshotError, match="verändert"):
        snapshots._copy_verified_file(
            origin,
            destination,
            {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()},
        )
    assert destination.read_bytes() == b"abc"
    assert reads == [3, 1]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_verified_copy_rejects_fifo_replacement_without_blocking(tmp_path, monkeypatch):
    origin, destination = tmp_path / "source", tmp_path / "copy"
    origin.write_bytes(b"abc")
    original_open = snapshots.os.open

    def replace_before_open(candidate, flags):
        origin.unlink()
        os.mkfifo(origin)
        return original_open(candidate, flags)

    monkeypatch.setattr(snapshots.os, "open", replace_before_open)
    with pytest.raises(snapshots.SnapshotError, match="verändert"):
        snapshots._copy_verified_file(
            origin,
            destination,
            {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()},
        )
    assert not destination.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_inventory_hash_rejects_fifo_replacement_without_blocking(
    tmp_path, monkeypatch
):
    path = tmp_path / "sample"
    path.write_bytes(b"sample")
    expected = path.stat()
    original_open = snapshots.os.open

    def replace_before_open(candidate, flags):
        path.unlink()
        os.mkfifo(path)
        return original_open(candidate, flags)

    monkeypatch.setattr(snapshots.os, "open", replace_before_open)
    with pytest.raises(snapshots.SnapshotError, match="verändert"):
        snapshots._hash_inventory_file(path, expected)


class ListingProcess:
    def __init__(self, data: bytes):
        self.stdout = io.BytesIO(data)
        self.returncode = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def install_listing(monkeypatch, data):
    process = ListingProcess(data)

    def spawn(command, **kwargs):
        assert kwargs["stderr"] == points.subprocess.DEVNULL
        assert kwargs["stdin"] == points.subprocess.DEVNULL
        return process

    monkeypatch.setattr(points.subprocess, "Popen", spawn)
    return process


def test_remote_browse_stops_after_display_limit_without_buffering_tail(monkeypatch):
    payload = json.dumps([{"Name": str(index)} for index in range(20_000)]).encode()
    process = install_listing(monkeypatch, payload)
    monkeypatch.setattr(points, "MAX_BROWSE_ITEMS", 2)
    items, truncated = points._remote_listing(["rclone", "dummy"])
    assert items == [{"Name": "0"}, {"Name": "1"}] and truncated
    assert process.killed and process.waited and process.stdout.closed


def test_remote_output_cap_kills_and_reaps_before_json_allocation(monkeypatch):
    process = install_listing(monkeypatch, b"[" + b" " * 1024)
    monkeypatch.setattr(points, "MAX_REMOTE_BYTES", 64)
    with pytest.raises(points.RecoveryPointError, match="Datenlimit"):
        points._remote_listing(["rclone", "dummy"])
    assert process.killed and process.waited and process.stdout.closed


def test_remote_inventory_caps_input_lines_even_for_duplicate_names(monkeypatch):
    process = install_listing(monkeypatch, b"file\t1\thash\n" * 10)
    monkeypatch.setattr(points, "MAX_DIFF_FILES", 2)
    inventory, truncated = points._remote_inventory("cloud:/dummy")
    assert inventory == {"file": (1, "hash")} and truncated
    assert process.killed and process.waited


def test_remote_version_directory_scan_is_bounded_and_reaps_child(monkeypatch):
    process = install_listing(monkeypatch, b"2026-10-25/\n2026-10-26/\n2026-10-27/\n")
    monkeypatch.setattr(points, "MAX_BROWSE_SCAN", 2)
    with pytest.raises(points.RecoveryPointError, match="Eintragslimit"):
        points._remote_directories("cloud:/dummy")
    assert process.killed and process.waited and process.stdout.closed


def test_remote_inventory_rejects_unbounded_single_line(monkeypatch):
    process = install_listing(monkeypatch, b"x" * 100)
    monkeypatch.setattr(points, "MAX_REMOTE_LINE_BYTES", 32)
    with pytest.raises(points.RecoveryPointError, match="lange Zeilen"):
        points._remote_inventory("cloud:/dummy")
    assert process.killed and process.waited


def test_remote_timeout_covers_read_before_eof(monkeypatch):
    killed = threading.Event()
    process = ListingProcess(b"")

    class BlockingOutput(io.BytesIO):
        def read(self, size=-1):
            assert killed.wait(timeout=2), "watchdog must run before EOF"
            return b""

    process.stdout = BlockingOutput()
    original_kill = process.kill

    def kill():
        original_kill()
        killed.set()

    process.kill = kill
    monkeypatch.setattr(points.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(points, "REMOTE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(points.RecoveryPointError, match="Zeitlimit"):
        points._remote_inventory("cloud:/dummy")
    assert process.killed and process.waited and process.stdout.closed


def test_remote_json_accepts_split_unicode_and_rejects_trailing_data(monkeypatch):
    data = json.dumps([{"Name": "März"}], ensure_ascii=False).encode()
    process = install_listing(monkeypatch, data)

    class BytewiseOutput(io.BytesIO):
        def read(self, size=-1):
            return super().read(1)

    process.stdout = BytewiseOutput(data)
    assert points._remote_listing(["rclone", "dummy"]) == ([{"Name": "März"}], False)
    process = install_listing(monkeypatch, b"[{}]unexpected")
    with pytest.raises(points.RecoveryPointError, match="ungültige Daten"):
        points._remote_listing(["rclone", "dummy"])
    assert process.killed and process.waited


@pytest.mark.parametrize("kind", ["points", "snapshots"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_watchdog_start_failure_still_closes_kills_and_waits(
    tmp_path, monkeypatch, kind, cleanup_fails
):
    process = install_listing(monkeypatch, b"[]")
    started_error = RuntimeError("dummy thread-start failure")
    timers = []

    class Timer:
        def __init__(self, *args, **kwargs):
            self.cancelled = False
            timers.append(self)

        def start(self):
            raise started_error

        def cancel(self):
            self.cancelled = True

    if cleanup_fails:

        def fail_wait(timeout):
            process.waited = True
            raise RuntimeError("dummy cleanup failure")

        process.wait = fail_wait
    monkeypatch.setattr(points.threading, "Timer", Timer)
    with pytest.raises(RuntimeError) as caught:
        if kind == "points":
            list(points._remote_chunks(["rclone", "dummy"]))
        else:
            snapshots._inventory_remote("cloud:/dummy", tmp_path, 1024)
    assert caught.value is started_error
    assert timers[0].cancelled
    assert process.killed and process.waited and process.stdout.closed


def test_staging_list_reports_invalid_overlap_as_http_400(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app.routes import api_recovery

    config, pair = recovery_config(tmp_path)
    config["paths"]["recovery_dir"] = pair["local"]
    monkeypatch.setattr(
        api_recovery, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    monkeypatch.setattr(api_recovery, "get_db", lambda: Mock())
    with pytest.raises(HTTPException) as caught:
        api_recovery.staging_items()
    assert caught.value.status_code == 400
    assert "außerhalb" in caught.value.detail
