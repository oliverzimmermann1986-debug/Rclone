"""Tests für den QEMU-Boot-Drill (gemockte Prozesse und Sockets)."""

import copy
import json
import os
import sqlite3
import stat
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException, Response

from app.db import Database
from app.jobs import boot_drill, rclone_sync, runtime_state
from app.jobs.rclone_sync import _SnapshotConfig
from app.routes import api_boot_drill, api_pbs

SNAPSHOTS = [
    {"backup-type": "vm", "backup-id": "100", "backup-time": 1700000000, "files": []},
    {
        "backup-type": "vm",
        "backup-id": "100",
        "backup-time": 1700100000,
        "files": [
            {"filename": "index.json.blob"},
            {"filename": "drive-scsi0.img.fidx", "size": 4096},
            {"filename": "drive-scsi1.img.fidx", "size": 4096},
        ],
    },
    {"backup-type": "vm", "backup-id": "100", "backup-time": 1600000000, "files": []},
]


def _config(tmp_path, **drill):
    return {
        "paths": {
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"timezone": "Europe/Berlin"},
        "pbs": {
            "enabled": True,
            "repository": "drill@pbs!tok@pbs.test:store",
            "password": "pbs-secret",
            "timeout_hours": 1,
            "boot_drill": {"group": "vm/100", **drill},
        },
    }


class FakeRun:
    def __init__(self, snapshots=SNAPSHOTS):
        self.snapshots = snapshots
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(self.snapshots), stderr=""
        )


class FakeProc:
    pid = 2**22 + 1  # über pid_max: killpg trifft nie einen echten Prozess

    def __init__(self, cmd, exit_code=None):
        self.cmd = self.args = cmd
        self.returncode = exit_code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakePopen:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.procs = []
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.kwargs = kwargs
        serial = next(a for a in cmd if a.startswith("file:"))[len("file:") :]
        Path(serial).write_bytes(b"x" * 3000 + b"login:")
        proc = FakeProc(cmd, self.exit_code)
        self.procs.append(proc)
        return proc


class FakeSock:
    def __init__(self, reply):
        self.reply = reply
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        return self.reply


def connect_after(attempts):
    calls = []

    def connect(path):
        calls.append(path)
        if len(calls) < attempts:
            raise FileNotFoundError(path)
        return FakeSock(b'{"return": {}}\n')

    connect.calls = calls
    return connect


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def time(self):
        return 1_700_000_000.0


@pytest.fixture
def env(monkeypatch):
    notes = []
    monkeypatch.setattr(boot_drill.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(boot_drill, "notify", lambda *a, **k: notes.append(a[0]))
    monkeypatch.setattr(boot_drill, "time", FakeClock())
    real_access = os.access
    state = {"kvm": True, "notes": notes, "restore_rc": 0, "restores": []}

    def restore(cmd, log_file, **kwargs):
        state["restores"].append((cmd, kwargs))
        Path(cmd[4]).write_bytes(b"\0" * 512)
        return state["restore_rc"]

    monkeypatch.setattr(boot_drill, "_run_rclone_command", restore)
    monkeypatch.setattr(
        boot_drill.os,
        "access",
        lambda p, m: state["kvm"] if p == "/dev/kvm" else real_access(p, m),
    )
    return state


def _scratch_left(tmp_path):
    return list((tmp_path / "temp").iterdir())


def test_latest_snapshot_default_archive_and_guest_ping(tmp_path, env):
    run, popen, connect = FakeRun(), FakePopen(), connect_after(3)

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path), run=run, popen=popen, connect=connect
    )

    assert summary["ok"] is True and summary["bootable"] is True
    assert summary["error"] is None
    assert summary["snapshot"] == "vm/100/2023-11-16T02:00:00Z"
    assert summary["archive"] == "drive-scsi0.img.fidx"
    assert summary["seconds_to_agent"] == 4.0
    assert summary["serial_tail"].endswith("login:")
    assert len(summary["serial_tail"]) == 2048
    assert summary["pairs"][0]["history_key"] == boot_drill.BOOT_DRILL_HISTORY_KEY
    list_cmd, list_kw = run.calls[0]
    assert list_cmd[1:6] == ["snapshot", "list", "vm/100", "--output-format", "json"]
    restore_cmd, restore_kw = env["restores"][0]
    assert restore_cmd[1:4] == ["restore", summary["snapshot"], "drive-scsi0.img.fidx"]
    assert Path(restore_cmd[4]).name == "disk.raw"
    assert Path(restore_cmd[4]).parent.name.startswith("sicherpfad-bootdrill-")
    assert restore_kw["timeout_sec"] == restore_kw["max_runtime_sec"] == 3600
    assert restore_kw["cancel_scope"] == boot_drill.PBS_CANCEL_SCOPE
    assert restore_kw["pair_name"] == boot_drill.RUN_NAME
    assert "pbs-secret" not in " ".join(restore_cmd)
    assert restore_kw["extra_env"]["PBS_PASSWORD"] == "pbs-secret"
    assert "PBS_PASSWORD" not in popen.kwargs["env"]
    assert popen.kwargs["start_new_session"] is True
    assert popen.procs[0].terminated is True
    assert Path(connect.calls[0]).name == "qga.sock"
    assert _scratch_left(tmp_path) == []
    assert env["notes"] == ["boot_drill_ok"]


@pytest.mark.parametrize("kvm", [True, False])
def test_qemu_arguments_enable_kvm_only_with_access(tmp_path, env, kvm):
    env["kvm"] = kvm
    seen = {}

    def popen(cmd, **kwargs):
        scratch = Path(cmd[cmd.index("-serial") + 1][len("file:") :]).parent
        seen.update(cmd=cmd, scratch=scratch)
        if os.name != "nt":
            seen["mode"] = stat.S_IMODE(scratch.stat().st_mode)
        return FakePopen()(cmd, **kwargs)

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path, memory_mb=2048),
        run=FakeRun(),
        popen=popen,
        connect=connect_after(1),
    )

    scratch = seen["scratch"]
    assert seen["cmd"] == [
        "/usr/bin/qemu-system-x86_64",
        "-m", "2048",
        "-display", "none",
        "-nodefaults",
        "-no-user-config",
        "-snapshot",
        "-drive", f"file={scratch / 'disk.raw'},format=raw,if=virtio",
        "-chardev", f"socket,id=qga0,path={scratch / 'qga.sock'},server=on,wait=off",
        "-device", "virtio-serial",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        "-serial", f"file:{scratch / 'serial.log'}",
        "-monitor", "none",
    ] + (["-enable-kvm"] if kvm else [])  # fmt: skip
    assert summary["kvm"] is kvm and summary["ok"] is True
    if os.name != "nt":
        assert seen["mode"] == 0o700


def test_guest_ping_timeout_fails_and_cleans_up(tmp_path, env):
    popen = FakePopen()
    sock = FakeSock(b"")  # verbunden, aber kein Agent antwortet

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path, timeout_seconds=10),
        run=FakeRun(),
        popen=popen,
        connect=lambda _path: sock,
    )

    assert summary["ok"] is False and summary["bootable"] is False
    assert "Guest Agent nach 10 s" in summary["error"]
    assert sock.sent.startswith(b'{"execute":"guest-ping"}\n')
    assert popen.procs[0].terminated is True
    assert _scratch_left(tmp_path) == []
    assert env["notes"] == ["boot_drill_error"]


def test_qemu_early_exit_reports_exit_code_and_kvm_hint(tmp_path, env):
    env["kvm"] = False

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path),
        run=FakeRun(),
        popen=FakePopen(exit_code=1),
        connect=connect_after(99),
    )

    assert summary["error"].startswith("QEMU vorzeitig beendet (Exit-Code 1)")
    assert "MemoryDenyWriteExecute" in summary["error"]
    assert _scratch_left(tmp_path) == []


def test_unexpected_exception_still_removes_scratch(tmp_path, env):
    popen = FakePopen()

    def connect(_path):
        raise ValueError("kaputt")

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path), run=FakeRun(), popen=popen, connect=connect
    )

    assert summary["ok"] is False
    assert summary["error"] == "Boot-Drill fehlgeschlagen: kaputt"
    assert popen.procs[0].terminated is True
    assert _scratch_left(tmp_path) == []


def test_restore_failure_cleans_up_without_starting_qemu(tmp_path, env):
    popen = FakePopen()
    env["restore_rc"] = 2

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path),
        run=FakeRun(),
        popen=popen,
        connect=connect_after(1),
    )

    assert "Exit-Code 2" in summary["error"]
    assert popen.procs == []
    assert _scratch_left(tmp_path) == []


def test_cancel_marker_during_restore_skips_qemu(tmp_path, env, monkeypatch):
    state = tmp_path / "runtime"
    for name, path in {
        "STATE_DIR": state,
        "RUN_FILE": state / "current-run.json",
        "PROCS_DIR": state / "processes",
        "CANCEL_FILE": state / "cancel.requested",
    }.items():
        monkeypatch.setattr(runtime_state, name, path)
    # Echter Watchdog-Pfad; nur der Kindprozess ist gefälscht.
    monkeypatch.setattr(
        boot_drill, "_run_rclone_command", rclone_sync._run_rclone_command
    )
    restores = []

    def restore_popen(cmd, **_kwargs):
        Path(cmd[4]).write_bytes(b"\0" * 512)
        runtime_state.request_cancel_marker(boot_drill.PBS_CANCEL_SCOPE)
        restores.append(FakeProc(cmd))
        return restores[-1]

    monkeypatch.setattr(rclone_sync.subprocess, "Popen", restore_popen)
    popen = FakePopen()

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path),
        run=FakeRun(),
        popen=popen,
        connect=connect_after(1),
    )

    assert summary["ok"] is False and summary["error"] == "abgebrochen"
    assert restores[0].terminated is True
    assert list(runtime_state.PROCS_DIR.iterdir()) == []
    assert popen.procs == []
    assert _scratch_left(tmp_path) == []


def test_stale_scratch_dirs_are_removed_at_start(tmp_path, env):
    temp = tmp_path / "temp"
    (temp / "sicherpfad-bootdrill-alt").mkdir(parents=True)
    (temp / "sicherpfad-bootdrill-alt" / "disk.raw").write_bytes(b"\0")
    (temp / "fremd").mkdir()

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path),
        run=FakeRun(),
        popen=FakePopen(),
        connect=connect_after(1),
    )

    assert summary["ok"] is True and summary["stale_cleaned"] == 1
    assert [p.name for p in temp.iterdir()] == ["fremd"]


@pytest.mark.parametrize("missing", ["qemu-system-x86_64", "proxmox-backup-client"])
def test_missing_binaries(tmp_path, env, monkeypatch, missing):
    monkeypatch.setattr(
        boot_drill.shutil,
        "which",
        lambda name: None if name == missing else f"/usr/bin/{name}",
    )
    run = FakeRun()

    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path), run=run, popen=FakePopen()
    )

    assert summary["error"] == f"{missing} nicht installiert"
    assert run.calls == []


def test_configured_archive_must_exist_in_snapshot(tmp_path, env):
    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path, archive="drive-virtio9.img.fidx"),
        run=FakeRun(),
        popen=FakePopen(),
    )

    assert "drive-virtio9.img.fidx ist nicht im Snapshot" in summary["error"]

    picked = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path, archive="drive-scsi1.img.fidx"),
        run=FakeRun(),
        popen=FakePopen(),
        connect=connect_after(1),
    )
    assert picked["archive"] == "drive-scsi1.img.fidx" and picked["ok"] is True


def test_invalid_group_is_rejected_before_any_command(tmp_path, env):
    run = FakeRun()
    summary = boot_drill.run_boot_drill(
        config_snapshot=_config(tmp_path, group="--repository=evil"), run=run
    )
    assert "group fehlt oder ist ungültig" in summary["error"]
    assert run.calls == []


class FakeHistoryDb:
    def __init__(self, last_success=None, last_result=None):
        self.history = {"last_success": last_success, "last_result": last_result}

    def pair_last_history(self, identities):
        return {key: self.history for key in identities}


def _berlin(*args):
    return datetime(*args, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


def test_boot_drill_due(tmp_path):
    cfg = _config(tmp_path, enabled=True, schedule="0 3 * * *")

    assert boot_drill.boot_drill_due(_config(tmp_path), FakeHistoryDb()) == {
        "due": False,
        "reason": "disabled",
    }
    first = boot_drill.boot_drill_due(
        cfg, FakeHistoryDb(), now=_berlin(2026, 7, 10, 12)
    )
    assert first["due"] is False
    assert first["reason"] == "waiting_for_first_schedule"
    assert first["history_key"] == boot_drill.BOOT_DRILL_HISTORY_KEY

    db = FakeHistoryDb(last_success={"ended_at": _berlin(2026, 7, 9, 3, 5)})
    assert boot_drill.boot_drill_due(cfg, db, now=_berlin(2026, 7, 10, 3, 30))["due"]
    assert not boot_drill.boot_drill_due(cfg, db, now=_berlin(2026, 7, 9, 12))["due"]


class FakeLease:
    def __init__(self):
        self.released = threading.Event()

    def release(self):
        self.released.set()


class FakeJobDb:
    def __init__(self):
        self.started = []
        self.finished = []
        self.audits = []

    def job_start(self, kind, **kwargs):
        self.started.append((kind, kwargs))
        return 7

    def job_finish(self, job_id, status, summary):
        self.finished.append((job_id, status, summary))

    def audit_add(self, event, **kwargs):
        self.audits.append(event)


class FakeConfig(_SnapshotConfig):
    def snapshot_with_revision(self):
        return copy.deepcopy(self.data), "rev-1"


def test_route_returns_409_when_pbs_lock_is_held(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_boot_drill, "get_config", lambda: FakeConfig(_config(tmp_path))
    )
    monkeypatch.setattr(api_boot_drill, "try_file_lock", lambda _scope: None)

    with pytest.raises(HTTPException) as exc:
        api_boot_drill.boot_drill_run()

    assert exc.value.status_code == 409


def test_route_starts_job_and_releases_lock(monkeypatch, tmp_path):
    db, lease = FakeJobDb(), FakeLease()
    monkeypatch.setattr(
        api_boot_drill, "get_config", lambda: FakeConfig(_config(tmp_path))
    )
    monkeypatch.setattr(api_boot_drill, "get_db", lambda: db)
    monkeypatch.setattr(api_boot_drill, "try_file_lock", lambda _scope: lease)
    monkeypatch.setattr(
        api_boot_drill, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    monkeypatch.setattr(
        api_boot_drill.boot_drill,
        "run_boot_drill",
        lambda **kwargs: {"ok": True, "trigger": kwargs["trigger"]},
    )

    assert api_boot_drill.boot_drill_run() == {"job_id": 7}
    assert lease.released.wait(5)

    kind, kwargs = db.started[0]
    assert kind == "bootdrill" and kwargs["exclusive_scope"] is True
    assert kwargs["attempts"][0]["history_key"] == "bootdrill:global"
    assert db.finished == [(7, "ok", {"ok": True, "trigger": "web"})]
    assert db.audits == ["boot_drill_requested"]


@pytest.fixture
def route_env(monkeypatch, tmp_path):
    db, lease = FakeJobDb(), FakeLease()
    state = tmp_path / "runtime"
    state.mkdir()
    for name, path in {
        "STATE_DIR": state,
        "RUN_FILE": state / "current-run.json",
        "PROCS_DIR": state / "processes",
        "CANCEL_FILE": state / "cancel.requested",
    }.items():
        monkeypatch.setattr(runtime_state, name, path)
    monkeypatch.setattr(rclone_sync, "_CANCEL_EVENTS", {})
    monkeypatch.setattr(api_boot_drill, "is_locked_down", lambda: False)
    monkeypatch.setattr(
        api_boot_drill, "get_config", lambda: FakeConfig(_config(tmp_path))
    )
    monkeypatch.setattr(api_boot_drill, "get_db", lambda: db)
    monkeypatch.setattr(api_boot_drill, "try_file_lock", lambda _scope: lease)
    monkeypatch.setattr(
        api_boot_drill, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    return db, lease


def test_manual_drill_clears_previous_pbs_cancel_under_scope_lock(
    route_env, monkeypatch
):
    db, lease = route_env
    scope = boot_drill.PBS_CANCEL_SCOPE
    marker = runtime_state._cancel_path(scope)
    marker.write_text("previous PBS cancellation", encoding="ascii")
    rclone_sync._cancel_event(scope).set()
    checks = []

    def run_drill(**_kwargs):
        checks.append((rclone_sync.is_cancelled(scope), lease.released.is_set()))
        return {"ok": True}

    monkeypatch.setattr(boot_drill, "run_boot_drill", run_drill)

    assert api_boot_drill.boot_drill_run() == {"job_id": 7}
    assert lease.released.wait(5)
    assert checks == [(False, False)]
    assert not marker.exists()
    assert db.finished == [(7, "ok", {"ok": True})]


def test_manual_drill_checks_lockdown_after_clearing_previous_cancel(
    route_env, monkeypatch
):
    db, lease = route_env
    scope = boot_drill.PBS_CANCEL_SCOPE
    rclone_sync._cancel_event(scope).set()
    observations = []

    def lockdown():
        observations.append(rclone_sync.is_cancelled(scope))
        return len(observations) > 1

    monkeypatch.setattr(api_boot_drill, "is_locked_down", lockdown)

    with pytest.raises(HTTPException) as exc:
        api_boot_drill.boot_drill_run()

    assert exc.value.status_code == 423
    assert observations == [True, False]
    assert db.started == []
    assert lease.released.is_set()


@pytest.mark.parametrize("log_update_fails", [False, True])
def test_completed_drill_keeps_terminal_intent_when_database_finish_is_busy(
    tmp_path, monkeypatch, log_update_fails
):
    path = tmp_path / "boot-drill.db"
    database = Database(path)
    job_id = database.job_start(boot_drill.JOB_KIND)
    lease = FakeLease()
    calls = []
    summary = {
        "ok": True,
        "bootable": True,
        "snapshot": "vm/100/fixture",
        "log_file": str(tmp_path / "boot-drill.log"),
    }

    def run_drill(**_kwargs):
        calls.append("drill")
        return summary

    def busy(_job_id):
        raise sqlite3.OperationalError("database is temporarily locked")

    if log_update_fails:
        monkeypatch.setattr(database, "job_set_log_file", lambda *_a: busy(job_id))
    monkeypatch.setattr(api_boot_drill, "get_db", lambda: database)
    monkeypatch.setattr(boot_drill, "run_boot_drill", run_drill)
    with monkeypatch.context() as blocked:
        blocked.setattr(database, "job_terminal_apply", busy)
        api_boot_drill._run_thread(job_id, lease, {})

    assert lease.released.is_set()
    assert database.job_get(job_id)["status"] == "running"
    assert database.job_terminal_pending(job_id)
    restarted = Database(path)
    assert restarted.job_terminal_recover_pending() == {
        "recovered": 1,
        "failed": 0,
        "recovered_job_ids": [job_id],
    }
    assert restarted.job_get(job_id)["status"] == "ok"
    assert restarted.job_get(job_id)["summary"] == summary
    assert calls == ["drill"]


def test_pbs_cancel_accepts_boot_drill_during_unregistered_listing(monkeypatch):
    database = FakeJobDb()
    database.job_running = lambda kind: (
        {"id": 7, "kind": boot_drill.JOB_KIND} if kind == boot_drill.JOB_KIND else None
    )
    signals = []
    monkeypatch.setattr(api_pbs, "get_db", lambda: database)
    monkeypatch.setattr(runtime_state, "active_processes", lambda _scope: [])

    def cancel(scope):
        signals.append(scope)
        return {"ok": True, "signal_persisted": True, "killed": 0}

    monkeypatch.setattr(rclone_sync, "cancel_job", cancel)
    result = api_pbs.pbs_cancel(Response())

    assert result["ok"] and result["signal_persisted"]
    assert signals == [boot_drill.PBS_CANCEL_SCOPE]
    assert database.audits == ["pbs_cancel_requested"]
