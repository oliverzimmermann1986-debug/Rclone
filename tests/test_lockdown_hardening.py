"""Lockdown-Härtung: zentrale Sperre, TOCTOU nach Lock-Erwerb, Subprozesse,
vorherige Scheduler-Pause und Obergrenze der PBS-Drossel."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config_store, conflicts, lockdown, push_notifications
from app import db as db_module
from app import scheduler_control
from app.auth_contract import NATIVE_LOGIN_PATH
from app.config_store import Config
from app.db import Database
from app.jobs import boot_drill, rclone_sync, scheduler_cli
from app.routes import api_boot_drill, api_jobs, api_pbs


def _boom(*_args, **_kwargs):
    raise AssertionError("darf nicht aufgerufen werden")


class _Lock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


# --- F06: zentrale Sperre in der Middleware ---------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "test-secret-which-is-long-enough-123456789",
                    "allowed_hosts": ["testserver"],
                },
                "paths": {"data_dir": str(tmp_path), "logs_dir": str(tmp_path)},
                "backup": {"enabled": True, "pairs": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "_config", Config(config_path))
    monkeypatch.setattr(db_module, "_db", Database(tmp_path / "app.db"))
    from app import main

    monkeypatch.setattr(main, "is_locked_down", lambda: True)
    return SimpleNamespace(http=TestClient(main.app), main=main)


@pytest.mark.parametrize(
    "path", ["/api/x", "/api/jobs/run", "/api/webauthn/authentication/verify"]
)
def test_middleware_blocks_state_changing_api_calls(client, path):
    response = client.http.post(path, json={})
    assert response.status_code == 423
    assert response.json()["detail"] == "Lockdown aktiv"


@pytest.mark.parametrize(
    "path", ["/api/lockdown/release", "/api/lockdown", "/login", NATIVE_LOGIN_PATH]
)
def test_middleware_keeps_password_login_and_release_open(client, path):
    assert client.http.post(path, json={}).status_code != 423


def test_middleware_leaves_reads_alone(client):
    assert client.http.get("/api/x").status_code == 404


def test_middleware_treats_unreadable_state_as_unlocked(client, monkeypatch, caplog):
    def broken():
        raise OSError("DB fehlt")

    monkeypatch.setattr(client.main, "is_locked_down", broken)
    assert client.http.post("/api/x", json={}).status_code != 423
    assert "Lockdown-Status nicht lesbar" in caplog.text


# --- F03: Prüfung unter dem Lock --------------------------------------------


@pytest.fixture
def held(monkeypatch):
    lock = _Lock()
    resets = []
    monkeypatch.setattr(api_jobs, "try_file_lock", lambda _scope: lock)
    monkeypatch.setattr(
        api_jobs, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    monkeypatch.setattr(api_jobs, "get_db", lambda: SimpleNamespace(job_start=_boom))
    monkeypatch.setattr(api_jobs, "is_locked_down", lambda *_a: True)
    monkeypatch.setattr(rclone_sync, "reset_cancel", lambda *_a: resets.append(1))
    return SimpleNamespace(lock=lock, resets=resets)


def test_reserve_backup_job_rechecks_lockdown_under_lock(held):
    assert api_jobs._locks["backup"].acquire(blocking=False)
    with pytest.raises(HTTPException) as info:
        api_jobs._reserve_backup_job("backup", config_revision="r")
    assert info.value.status_code == 423
    assert held.lock.released and held.resets == [1]
    assert not api_jobs._locks["backup"].locked()


def test_definition_batch_scope_rechecks_lockdown_under_lock(held):
    with pytest.raises(HTTPException) as info:
        api_jobs._acquire_definition_batch_scope()
    assert info.value.status_code == 423
    assert held.lock.released
    assert not api_jobs._locks["backup"].locked()


def test_resume_pending_batches_does_nothing_during_lockdown(monkeypatch):
    monkeypatch.setattr(api_jobs, "is_locked_down", lambda *_a: True)
    monkeypatch.setattr(
        api_jobs, "get_db", lambda: SimpleNamespace(job_batches_pending=_boom)
    )
    assert api_jobs.resume_pending_definition_batches() == 0


def _late_lockdown():
    """Erst frei, nach dem Lock-Erwerb gesperrt."""
    states = iter([False, True])
    return lambda *_a: next(states)


def test_pbs_run_rechecks_lockdown_under_lock(monkeypatch):
    lock = _Lock()
    monkeypatch.setattr(api_pbs, "is_locked_down", _late_lockdown())
    monkeypatch.setattr(
        api_pbs,
        "get_config",
        lambda: SimpleNamespace(snapshot_with_revision=lambda: ({}, "r")),
    )
    monkeypatch.setattr(
        api_pbs.pbs_backup, "pbs_settings", lambda _c: {"enabled": True}
    )
    monkeypatch.setattr(api_pbs.pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    monkeypatch.setattr(api_pbs.pbs_backup, "pbs_targets", lambda _s: [{"name": "vm"}])
    monkeypatch.setattr(api_pbs, "pbs_history_key", lambda *_a: "k")
    monkeypatch.setattr(api_pbs, "try_file_lock", lambda _scope: lock)
    monkeypatch.setattr(api_pbs, "get_db", lambda: SimpleNamespace(job_start=_boom))
    monkeypatch.setattr(
        api_pbs, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    monkeypatch.setattr(rclone_sync, "reset_cancel", lambda *_a: None)

    with pytest.raises(HTTPException) as info:
        api_pbs.pbs_run(api_pbs.PbsRunPayload())
    assert info.value.status_code == 423
    assert lock.released and not api_pbs._lock.locked()


def test_boot_drill_run_rechecks_lockdown_under_lock(monkeypatch):
    lock = _Lock()
    monkeypatch.setattr(api_boot_drill, "is_locked_down", _late_lockdown())
    monkeypatch.setattr(
        api_boot_drill,
        "get_config",
        lambda: SimpleNamespace(snapshot_with_revision=lambda: ({}, "r")),
    )
    monkeypatch.setattr(api_boot_drill, "try_file_lock", lambda _scope: lock)
    monkeypatch.setattr(
        api_boot_drill, "get_db", lambda: SimpleNamespace(job_start=_boom)
    )
    monkeypatch.setattr(
        api_boot_drill, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )

    with pytest.raises(HTTPException) as info:
        api_boot_drill.boot_drill_run()
    assert info.value.status_code == 423
    assert lock.released


# --- F03 (d) und F08: Scheduler ----------------------------------------------


class _SchedulerDb:
    def __init__(self):
        self.started: list[str] = []
        self.runtime: dict = {}

    def job_start(self, kind, **_kwargs):
        self.started.append(kind)
        return 7

    def runtime_get(self, key, default=None):
        return self.runtime.get(key, default)

    def runtime_set(self, key, value):
        self.runtime[key] = value

    def runtime_delete(self, key):
        self.runtime.pop(key, None)


@pytest.fixture
def tick(tmp_path, monkeypatch, caplog):
    env = SimpleNamespace(db=_SchedulerDb(), locked=False, locks=[], lock_free=True)
    data = {
        "backup": {"enabled": True},
        "paths": {"logs_dir": str(tmp_path)},
        "pbs": {"enabled": True},  # sonst greift die Wartungsdrossel nicht
    }
    config = SimpleNamespace(
        data=data, revision="r", get=lambda *_k, default=None: default
    )

    @contextmanager
    def lock(name):
        env.locks.append(name)
        yield object() if env.lock_free else None

    patches = {
        "get_config": lambda: config,
        "get_db": lambda: env.db,
        "_configure_logging": lambda _path=None: None,
        "_reconcile_available_scopes": lambda _db: None,
        "check_overdue": lambda *_a: [],
        "is_locked_down": lambda _db: env.locked,
        "scheduler_state": lambda _db: {"paused": False},
        "file_lock_or_none": lock,
        "reconcile_locked_scope": lambda *_a, **_k: {"safe": True},
        # Simuliert einen Lockdown, der genau beim Phasenstart aktiviert wird.
        "reset_cancel": lambda *_a: setattr(env, "locked", True),
        "find_due_pairs": lambda *_a: ([], []),
        "find_due_pbs_targets": lambda *_a: ([], []),
        "restore_test_due": lambda *_a: {"due": False},
        "bitrot_scrub_due": lambda *_a: {"due": False},
        "boot_drill_due": lambda *_a: {"due": False},
        "maintenance_active": lambda _s: {"active": False, "tasks": []},
        "run_job": _boom,
        "run_pbs_backup": _boom,
        "run_restore_test": _boom,
        "run_bitrot_scrub": _boom,
        "run_boot_drill": _boom,
    }
    for name, value in patches.items():
        monkeypatch.setattr(scheduler_cli, name, value)
    monkeypatch.setattr(push_notifications, "dispatch_pending_pushes", lambda **_k: {})
    caplog.set_level(logging.INFO, logger="scheduler_cli")
    return env


PBS_DUE = (
    ["vm"],
    [
        {
            "name": "vm",
            "run_name": "pbs:vm",
            "history_key": "k",
            "scheduled_slot": "s",
            "due": True,
        }
    ],
)


@pytest.mark.parametrize(
    "due_patch,phase",
    [
        ({"find_due_pairs": lambda *_a: (["docs"], [{"name": "docs", "due": True}])}, "rclone-Phase"),
        ({"find_due_pbs_targets": lambda *_a: PBS_DUE}, "PBS-Phase"),
        ({"restore_test_due": lambda *_a: {"due": True}}, "Restore-Drill"),
        ({"bitrot_scrub_due": lambda *_a: {"due": True}}, "Bitrot-Scrub"),
        ({"boot_drill_due": lambda *_a: {"due": True}}, "Boot-Drill"),
    ],
)  # fmt: skip
def test_scheduler_skips_phase_when_lockdown_starts_meanwhile(
    tick, monkeypatch, caplog, due_patch, phase
):
    for name, value in due_patch.items():
        monkeypatch.setattr(scheduler_cli, name, value)

    assert scheduler_cli.main() == 0
    assert tick.db.started == []
    assert f"Lockdown aktiv: {phase} übersprungen" in caplog.text


@pytest.mark.parametrize(
    "active,age,deferred",
    [
        (True, None, True),
        (True, 6 * 3600 - 60, True),
        (True, 6 * 3600 + 60, False),
        (False, 7 * 3600, False),
    ],
)
def test_pbs_throttle_defers_at_most_six_hours(
    tick, monkeypatch, caplog, active, age, deferred
):
    tick.lock_free = False  # nur sehen, ob die rclone-Phase den Lock anfragt
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pairs",
        lambda *_a: (["docs"], [{"name": "docs", "due": True}]),
    )
    monkeypatch.setattr(
        scheduler_cli, "maintenance_active", lambda _s: {"active": active, "tasks": []}
    )
    if age is not None:
        tick.db.runtime["pbs_throttle_since"] = time.time() - age
    before = time.time()

    scheduler_cli.main()

    assert tick.locks == ([] if deferred else ["backup"])
    since = tick.db.runtime.get("pbs_throttle_since")
    if not active:
        assert since is None
    elif age is None:
        assert since >= before
    assert ("nicht länger zurückgestellt" in caplog.text) is (active and not deferred)


# --- F04: Subprozesse der neuen Module -----------------------------------------


def test_resolve_conflict_aborts_between_steps_on_lockdown(monkeypatch):
    def item(name):
        return {
            "Path": name,
            "Name": name,
            "Size": 1,
            "ModTime": "2026-10-01T10:00:00Z",
        }

    listings = {
        "gd:docs": [item("a.conflict1.txt")],
        "/data/docs": [item("a.conflict2.txt")],
    }
    mutations = []

    def run(cmd, **_kwargs):
        if cmd[1] == "lsjson":
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps(listings[cmd[-1]]), ""
            )
        mutations.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(lockdown, "is_locked_down", _late_lockdown())
    pair = {"name": "docs", "remote": "gd:", "local": "/data", "direction": "bisync"}

    result = conflicts.resolve_conflict(pair, "docs/a.txt", "keep_side1", run=run)

    assert result["completed"] is False and result["aborted"] == "lockdown"
    assert len(result["steps"]) == len(mutations) == 1


class _Proc:
    pid = 2**22 + 1  # über pid_max: killpg trifft nie einen echten Prozess
    returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def sendall(self, _data):
        pass

    def recv(self, _size):
        return b'{"return": {}}\n'


@pytest.mark.parametrize("locked", [False, True])
def test_boot_drill_registers_qemu_and_stops_on_lockdown(tmp_path, monkeypatch, locked):
    snapshot = {
        "backup-type": "vm",
        "backup-id": "100",
        "backup-time": 1700000000,
        "files": [{"filename": "drive-scsi0.img.fidx", "size": 4096}],
    }

    def run(cmd, **_kwargs):
        if cmd[1] == "snapshot":
            return subprocess.CompletedProcess(cmd, 0, json.dumps([snapshot]), "")
        return subprocess.CompletedProcess(cmd, 0)

    procs, registered, unregistered = [], [], []

    def popen(_cmd, **_kwargs):
        procs.append(_Proc())
        return procs[-1]

    monkeypatch.setattr(boot_drill.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(boot_drill, "notify", lambda *_a, **_k: None)
    monkeypatch.setattr(boot_drill, "is_locked_down", lambda: locked)
    # Restore-Abbruchpfad prüft tests/test_boot_drill.py; hier zählt nur QEMU.
    monkeypatch.setattr(boot_drill, "_run_rclone_command", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        boot_drill.runtime_state,
        "register_process",
        lambda pid, **kw: registered.append((pid, kw["scope"], kw["executable"])),
    )
    monkeypatch.setattr(
        boot_drill.runtime_state,
        "unregister_process",
        lambda pid, **_kw: unregistered.append(pid),
    )
    config = {
        "paths": {
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "pbs": {
            "enabled": True,
            "repository": "drill@pbs!tok@pbs.test:store",
            "boot_drill": {"group": "vm/100"},
        },
    }

    summary = boot_drill.run_boot_drill(
        config_snapshot=config, run=run, popen=popen, connect=lambda _p: _Sock()
    )

    assert list((tmp_path / "temp").iterdir()) == []
    if locked:
        assert summary["error"] == "Lockdown aktiv"
        assert procs == registered == unregistered == []
    else:
        assert summary["ok"] is True
        pid = _Proc.pid
        assert registered == [(pid, "pbs", "/usr/bin/qemu-system-x86_64")]
        assert unregistered == [pid]


# --- F07: vorherige Pause ------------------------------------------------------


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(db_module, "_db", database)
    monkeypatch.setattr(
        lockdown,
        "get_config",
        lambda: SimpleNamespace(get=lambda *_k, default=None: []),
    )
    monkeypatch.setattr(rclone_sync, "cancel_job", lambda scope: {"ok": True})
    from app import auth
    from app.jobs import runtime_state

    monkeypatch.setattr(
        runtime_state, "terminate_active_processes", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(auth, "bump_session_version", lambda: 2)
    monkeypatch.setattr(lockdown, "send_push_notifications", lambda *_a, **_k: {})
    return database


@pytest.mark.parametrize(
    "pause_seconds,restored", [(None, False), (3600, True), (-1, False)]
)
def test_release_restores_previous_pause(
    lock_env, monkeypatch, pause_seconds, restored
):
    if pause_seconds is not None:
        until = time.time() + max(pause_seconds, 60)
        scheduler_control.pause_scheduler(until=until, reason="Umzug", db=lock_env)
    lockdown.activate_lockdown(actor="admin", reason="x", run=_boom)
    # Zweite Aktivierung darf die ursprüngliche Pause nicht überschreiben.
    lockdown.activate_lockdown(actor="admin", reason="y", run=_boom)
    if pause_seconds == -1:  # vorherige Pause ist während des Lockdowns abgelaufen
        state = lockdown.lockdown_state(lock_env)
        state["previous_pause"]["until"] = time.time() - 1
        lock_env.runtime_set("lockdown", state)

    released = lockdown.release_lockdown(actor="admin")

    assert released["scheduler"] == {"restored_pause": restored}
    after = scheduler_control.scheduler_state(lock_env)
    assert after["paused"] is restored
    if restored:
        assert after["until"] == pytest.approx(until) and after["reason"] == "Umzug"
