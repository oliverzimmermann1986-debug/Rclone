import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import recovery_snapshots
from app.db import Database
from app.jobs import locks, rclone_sync, recovery_lifecycle, runtime_state
from app.jobs import selective_restore
from app.jobs.job_lifecycle import BACKUP_KINDS, reconcile_locked_scope
from app.routes import api_jobs, api_recovery


@pytest.fixture
def env(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(runtime_state, "STATE_DIR", runtime)
    monkeypatch.setattr(runtime_state, "RUN_FILE", runtime / "run.json")
    monkeypatch.setattr(runtime_state, "CANCEL_FILE", runtime / "cancel")
    monkeypatch.setattr(runtime_state, "PROCS_DIR", runtime / "processes")
    rclone_sync.reset_cancel()
    database = Database(tmp_path / "jobs.db")
    target = tmp_path / "target"
    target.mkdir()
    (target / "one.txt").write_bytes(b"one")
    (target / "two.txt").write_bytes(b"two")
    pair = {
        "id": "photos",
        "name": "Fotos",
        "direction": "push",
        "local": str(tmp_path / "live"),
        "remote": str(target),
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "recovery_dir": str(tmp_path / "staging"),
        },
        "backup": {"pairs": [pair]},
    }
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    monkeypatch.setattr(
        api_recovery, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    notices = []
    monkeypatch.setattr(
        selective_restore, "notify", lambda *args, **kwargs: notices.append(args)
    )
    yield SimpleNamespace(db=database, config=config, pair=pair, notices=notices)
    rclone_sync.reset_cancel()


def assert_scope_free():
    lease = locks.try_file_lock("backup")
    assert lease is not None
    lease.release()


def test_running_recovery_keeps_scope_against_reconciliation_and_backup_start(
    env, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = api_recovery.run_snapshot_capture
    failures = []

    def work(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(api_recovery, "run_snapshot_capture", work)
    background = BackgroundTasks()
    response = api_recovery.start_snapshot(
        api_recovery.SnapshotRequest(identity="photos"), background
    )
    job_id = response["job_id"]

    def dispatch():
        try:
            asyncio.run(background())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=dispatch)
    worker.start()
    try:
        assert entered.wait(5)
        # This is the same prerequisite used by startup and scheduler recovery.
        with locks.file_lock_or_none("backup") as acquired:
            assert acquired is None
        assert env.db.job_get(job_id)["status"] == "running"
        with pytest.raises(HTTPException) as busy:
            api_recovery._start_recovery_job()
        assert busy.value.status_code == 409
        assert api_jobs._locks["backup"].acquire(blocking=False)
        with pytest.raises(HTTPException) as backup_busy:
            api_jobs._reserve_backup_job("backup", config_revision="test")
        assert backup_busy.value.status_code == 409
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert failures == []
    assert env.db.job_get(job_id)["status"] == "ok"
    assert_scope_free()


@pytest.mark.parametrize("phase", ["reservation", "enqueue", "worker", "audit"])
def test_recovery_failures_release_scope(env, monkeypatch, phase):
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    if phase == "reservation":
        monkeypatch.setattr(env.db, "job_start", fail)
        with pytest.raises(RuntimeError):
            api_recovery._start_recovery_job()
    else:
        database, job_id, lease = api_recovery._start_recovery_job()
        if phase == "worker":
            api_recovery._run_recovery_job(database, job_id, lease, fail)
        else:
            background = BackgroundTasks()
            if phase == "enqueue":
                monkeypatch.setattr(background, "add_task", fail)
            else:
                monkeypatch.setattr(database, "audit_add", fail)
            with pytest.raises(RuntimeError):
                api_recovery._queue_recovery(background, database, job_id, lease, fail)
        assert env.db.job_get(job_id)["status"] == "error"
    assert_scope_free()


def fake_restore(monkeypatch, *, after_copy=None, after_check=None):
    payload = b"verified selection"

    def command(args, *, timeout):
        output = ""
        if args[1] == "lsjson":
            output = json.dumps(
                [
                    {
                        "Path": "proof.txt",
                        "Size": len(payload),
                        "Hashes": {"SHA-256": hashlib.sha256(payload).hexdigest()},
                    }
                ]
            )
        elif args[1] == "copy":
            (Path(args[-1]) / "proof.txt").write_bytes(payload)
            if after_copy:
                after_copy()
        elif args[1] == "check" and after_check:
            after_check()
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(selective_restore, "_run", command)


def run_selection(env, job_id):
    return selective_restore.run_selective_restore(
        env.db, env.config, env.pair, ["proof.txt"], max_total_mb=1, job_id=job_id
    )


def test_crashed_selection_remains_visible_and_projects_stale_job(env, monkeypatch):
    class Crash(BaseException):
        pass

    def crash():
        item = selective_restore.list_staging(env.db, env.config)[0]
        assert item["status"] == "running"
        with pytest.raises(ValueError, match="abbrechen"):
            selective_restore.remove_staging(env.db, env.config, item["id"])
        raise Crash()

    fake_restore(monkeypatch, after_copy=crash)
    job_id = env.db.job_start("recovery")
    with pytest.raises(Crash):
        run_selection(env, job_id)
    with locks.file_lock_or_none("backup") as lease:
        assert lease is not None
        assert reconcile_locked_scope(env.db, scope="backup", kinds=BACKUP_KINDS)[
            "safe"
        ]
    item = selective_restore.list_staging(env.db, env.config)[0]
    assert item["status"] == "stale"
    assert item["exists"] and item["cleanup_required"]
    assert item["verified"] is False
    assert (Path(item["staging_path"]) / "proof.txt").is_file()
    assert env.notices == []


@pytest.mark.parametrize("when", ["copy", "check"])
def test_cancelled_selection_never_publishes_ready(env, monkeypatch, when):
    fake_restore(monkeypatch, **{f"after_{when}": runtime_state.request_cancel_marker})
    job_id = env.db.job_start("recovery")
    result = run_selection(env, job_id)
    assert result["status"] == "cancelled" and not result["verified"]
    assert env.db.job_get(job_id)["status"] == "cancelled"
    assert not Path(result["staging_path"]).parent.exists()
    assert env.notices == []


def test_unconfirmed_terminal_result_never_publishes_ready(env, monkeypatch):
    fake_restore(monkeypatch)
    job_id = env.db.job_start("recovery")
    monkeypatch.setattr(env.db, "job_finish_external", lambda *args: False)
    with pytest.raises(recovery_lifecycle.RecoveryPersistenceError):
        run_selection(env, job_id)
    item = selective_restore.list_staging(env.db, env.config)[0]
    assert item["status"] == "running" and item["verified"] is False
    assert env.notices == []


def test_confirmed_job_recovers_ready_projection_after_staging_save_failure(
    env, monkeypatch
):
    fake_restore(monkeypatch)
    original = selective_restore._save_record

    def save(db, record):
        if record["status"] == "ready":
            raise OSError("interrupted after terminal database commit")
        original(db, record)

    monkeypatch.setattr(selective_restore, "_save_record", save)
    job_id = env.db.job_start("recovery")
    with pytest.raises(OSError):
        run_selection(env, job_id)
    assert env.db.job_get(job_id)["status"] == "ok"
    item = selective_restore.list_staging(env.db, env.config)[0]
    assert item["status"] == "ready" and item["verified"] is True
    assert item["exists"]
    assert env.notices == []


def test_snapshot_restore_observes_cancel_between_files(env, monkeypatch):
    point = recovery_snapshots.capture_snapshot(env.config, env.pair, max_total_mb=1)
    original = recovery_snapshots.copy_file
    copied = []

    def copy(source, target):
        original(source, target)
        copied.append(source)
        runtime_state.request_cancel_marker()

    monkeypatch.setattr(recovery_snapshots, "copy_file", copy)
    job_id = env.db.job_start("recovery")
    result = recovery_snapshots.run_snapshot_restore(
        env.db,
        env.config,
        env.pair,
        point_id=point["id"],
        max_total_mb=1,
        job_id=job_id,
    )
    assert len(copied) == 1
    assert result["status"] == "cancelled" and not result["verified"]
    assert env.db.job_get(job_id)["status"] == "cancelled"
    assert not Path(result["staging_path"]).parent.exists()


def test_registered_child_is_reaped_and_unregistered_on_cancel(env, monkeypatch):
    registered = threading.Event()
    processes, errors = [], []
    register = rclone_sync._register_proc

    def observe(process, **kwargs):
        register(process, **kwargs)
        processes.append(process)
        registered.set()

    monkeypatch.setattr(rclone_sync, "_register_proc", observe)

    def run():
        try:
            recovery_lifecycle.run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=40
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert registered.wait(5)
        assert list(runtime_state.PROCS_DIR.glob("*.json"))
        runtime_state.request_cancel_marker()
    finally:
        runtime_state.request_cancel_marker()
        worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(
        errors[0], recovery_lifecycle.RecoveryCancelled
    )
    assert processes[0].poll() is not None
    assert list(runtime_state.PROCS_DIR.glob("*.json")) == []


def test_cancel_before_background_work_finishes_job_and_releases_scope(env):
    database, job_id, lease = api_recovery._start_recovery_job()
    runtime_state.request_cancel_marker()
    calls = []
    api_recovery._run_recovery_job(
        database, job_id, lease, lambda *_a, **_kw: calls.append(True)
    )
    assert calls == []
    assert env.db.job_get(job_id)["status"] == "cancelled"
    assert_scope_free()


def test_cancel_marker_does_not_disable_read_only_inventory(env):
    runtime_state.request_cancel_marker()
    files, _directories = recovery_snapshots._inventory_local(
        Path(env.pair["remote"]), limit_bytes=1024, hashes=True
    )
    assert set(files) == {"one.txt", "two.txt"}


def test_staging_delete_cannot_touch_data_while_scope_is_owned(env, monkeypatch):
    fake_restore(monkeypatch)
    job_id = env.db.job_start("recovery")
    result = run_selection(env, job_id)
    lease = locks.try_file_lock("backup")
    assert lease is not None
    try:
        with pytest.raises(recovery_lifecycle.RecoveryBusy):
            selective_restore.remove_staging(env.db, env.config, result["id"])
        assert (Path(result["staging_path"]) / "proof.txt").is_file()
    finally:
        lease.release()
    assert selective_restore.remove_staging(env.db, env.config, result["id"])


def test_durable_terminal_intent_is_replayed_before_scope_reconciliation(
    env, monkeypatch
):
    fake_restore(monkeypatch)
    database, job_id, lease = api_recovery._start_recovery_job()
    apply = database.job_terminal_apply

    def unavailable(*_args):
        raise sqlite3.OperationalError("temporarily locked")

    monkeypatch.setattr(database, "job_terminal_apply", unavailable)
    api_recovery._run_recovery_job(
        database,
        job_id,
        lease,
        selective_restore.run_selective_restore,
        env.config,
        env.pair,
        ["proof.txt"],
        max_total_mb=1,
    )
    assert database.job_get(job_id)["status"] == "running"
    assert database.job_terminal_pending(job_id)
    assert env.notices == []
    assert_scope_free()
    monkeypatch.setattr(database, "job_terminal_apply", apply)
    _, next_job_id, next_lease = api_recovery._start_recovery_job()
    try:
        assert database.job_get(job_id)["status"] == "ok"
        assert not database.job_terminal_pending(job_id)
        item = selective_restore.list_staging(database, env.config)[0]
        assert item["status"] == "ready" and item["verified"]
        database.job_finish(next_job_id, "cancelled")
    finally:
        next_lease.release()


def test_terminal_cancel_race_retains_visible_unverified_staging(env, monkeypatch):
    fake_restore(monkeypatch)
    finish = selective_restore.finish_job

    def cancel_before_finish(*args):
        runtime_state.request_cancel_marker()
        return finish(*args)

    monkeypatch.setattr(selective_restore, "finish_job", cancel_before_finish)
    database, job_id, lease = api_recovery._start_recovery_job()
    api_recovery._run_recovery_job(
        database,
        job_id,
        lease,
        selective_restore.run_selective_restore,
        env.config,
        env.pair,
        ["proof.txt"],
        max_total_mb=1,
    )
    assert database.job_get(job_id)["status"] == "cancelled"
    item = selective_restore.list_staging(database, env.config)[0]
    assert item["status"] == "cancelled" and not item["verified"]
    assert item["cleanup_required"] and item["exists"]
    assert env.notices == []
    assert_scope_free()


def unkillable_child(monkeypatch, *, on_spawn):
    processes = []

    class Process:
        def __init__(self, args, **kwargs):
            self.args, self.pid, self.returncode = args, 99999999, None
            processes.append(self)
            on_spawn(args)

        def poll(self):
            return self.returncode

    def cannot_terminate(*_args, **_kwargs):
        raise runtime_state.ProcessTerminationError("child exit remains unconfirmed")

    monkeypatch.setattr(recovery_lifecycle.subprocess, "Popen", Process)
    monkeypatch.setattr(rclone_sync, "_terminate_proc", cannot_terminate)
    monkeypatch.setattr(
        runtime_state,
        "active_processes",
        lambda _scope="backup": [
            {"pid": process.pid, "scope": "backup"}
            for process in processes
            if process.poll() is None
        ],
    )
    return processes


def test_unconfirmed_child_exit_retains_staging_and_delete_returns_409(
    env, monkeypatch
):
    def created(command):
        (Path(command[-1]) / "proof.txt").write_bytes(b"partial download")

    processes = unkillable_child(monkeypatch, on_spawn=created)
    monkeypatch.setattr(
        selective_restore,
        "_expected_manifest",
        lambda *_a, **_kw: {
            "proof.txt": {"bytes": 16, "algorithm": "sha256", "checksum": "0" * 64}
        },
    )
    monkeypatch.setattr(
        selective_restore,
        "_run",
        lambda command, **kwargs: recovery_lifecycle.run_command(command, timeout=0),
    )
    job_id = env.db.job_start("recovery")
    try:
        result = run_selection(env, job_id)
        assert result["status"] == "error" and not result["verified"]
        assert result["cleanup_required"]
        assert (Path(result["staging_path"]) / "proof.txt").exists()
        assert list(runtime_state.PROCS_DIR.glob("*.json"))
        assert env.db.job_get(job_id)["status"] == "error"
        monkeypatch.setattr(api_recovery, "require_reauthentication", lambda *_a: None)
        with pytest.raises(HTTPException) as busy:
            api_recovery.delete_staging(
                result["id"],
                api_recovery.RemoveStagingRequest(current_password="test-password"),
                SimpleNamespace(),
                user="owner",
            )
        assert busy.value.status_code == 409
        assert (Path(result["staging_path"]) / "proof.txt").exists()
        assert_scope_free()
    finally:
        for process in processes:
            process.returncode = 0
            rclone_sync._unregister_proc(process)
    assert selective_restore.remove_staging(env.db, env.config, result["id"])


def test_unconfirmed_snapshot_child_preserves_workspace_registry(env, monkeypatch):
    from app.jobs import restore_workspace

    pair = {**env.pair, "remote": "cloud:Photos"}
    monkeypatch.setattr(
        recovery_snapshots,
        "_inventory_remote",
        lambda *_a, **_kw: ({"proof.txt": {"size": 16, "modified": "fixture"}}, []),
    )

    def created(command):
        (Path(command[-1]) / "proof.txt").write_bytes(b"partial download")

    processes = unkillable_child(monkeypatch, on_spawn=created)
    monkeypatch.setattr(
        recovery_snapshots,
        "_run",
        lambda command, **kwargs: recovery_lifecycle.run_command(command, timeout=0),
    )
    job_id = env.db.job_start("recovery")
    try:
        result = recovery_snapshots.run_snapshot_capture(
            env.db, env.config, pair, max_total_mb=1, job_id=job_id
        )
        assert result["ok"] is False
        root = recovery_snapshots.snapshot_root(env.config)
        workdirs = list(root.glob("restore-snapshot-capture-*"))
        assert len(workdirs) == 1
        assert (workdirs[0] / "data" / "proof.txt").exists()
        assert len(list((root / restore_workspace.REGISTRY).glob("*.json"))) == 1
        assert list(runtime_state.PROCS_DIR.glob("*.json"))
        assert list(root.glob("full-*")) == []
    finally:
        for process in processes:
            process.returncode = 0
            rclone_sync._unregister_proc(process)
    assert restore_workspace.cleanup_orphaned_workspaces(root)["removed"] == 1
    assert not list(root.glob("restore-snapshot-capture-*"))
    assert not list((root / restore_workspace.REGISTRY).glob("*.json"))


def test_staging_cleanup_fails_closed_when_process_registry_is_unreadable(
    env, monkeypatch
):
    fake_restore(monkeypatch)
    result = run_selection(env, env.db.job_start("recovery"))

    def unreadable(*_args):
        raise OSError("registry unreadable")

    monkeypatch.setattr(runtime_state, "active_processes", unreadable)
    with pytest.raises(recovery_lifecycle.RecoveryBusy):
        selective_restore.remove_staging(env.db, env.config, result["id"])
    assert (Path(result["staging_path"]) / "proof.txt").exists()
    assert_scope_free()
