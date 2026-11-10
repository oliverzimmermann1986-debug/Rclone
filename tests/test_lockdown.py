from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth, lockdown, scheduler_control
from app import db as db_module
from app.db import Database
from app.jobs import rclone_sync, runtime_state
from app.routes import api_lockdown

PASSWORD = "geheim-Passwort-123"


def _pair(mountpoint: str, *, enabled: bool = True, require: bool = True) -> dict:
    return {
        "name": mountpoint,
        "enabled": enabled,
        "require_mountpoint": require,
        "mountpoint": mountpoint,
    }


def _fake_run(outcomes: dict):
    def run(cmd, **kwargs):
        run.attempted.append(cmd[-1])
        outcome = outcomes[cmd[-1]]
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(returncode=outcome[0], stderr=outcome[1])

    run.attempted = []
    return run


@pytest.fixture
def env(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(db_module, "_db", database)
    calls: list[tuple] = []
    pairs: list[dict] = []
    config = SimpleNamespace(get=lambda *keys, default=None: pairs)
    monkeypatch.setattr(lockdown, "get_config", lambda: config)
    real_pause = scheduler_control.pause_scheduler

    def cancel(scope):
        calls.append(("cancel", scope, lockdown.is_locked_down(database)))
        return {"ok": True, "killed": 0, "scope": scope}

    def kill(graceful_sec, *, scope):
        calls.append(("kill", scope, graceful_sec))
        return 0

    def pause(**kwargs):
        calls.append(("pause",))
        return real_pause(**kwargs)

    def bump():
        audited = bool(database.audit_list(event_type="lockdown_activated"))
        calls.append(("bump", audited))
        return 2

    monkeypatch.setattr(rclone_sync, "cancel_job", cancel)
    monkeypatch.setattr(runtime_state, "terminate_active_processes", kill)
    monkeypatch.setattr(scheduler_control, "pause_scheduler", pause)
    monkeypatch.setattr(auth, "bump_session_version", bump)
    return SimpleNamespace(db=database, calls=calls, pairs=pairs)


def test_activate_orders_steps_and_survives_remount_failures(env):
    env.pairs.extend(
        [
            _pair("/mnt/ok"),
            _pair("/mnt/ok/"),
            _pair("/mnt/denied"),
            _pair("/mnt/slow"),
            _pair("/mnt/nobin"),
            _pair("relative/path"),
            _pair("/"),
            _pair("/mnt/disabled", enabled=False),
            _pair("/mnt/optional", require=False),
        ]
    )
    run = _fake_run(
        {
            "/mnt/ok": (0, ""),
            "/mnt/denied": (32, "mount: /mnt/denied: must be superuser to use mount."),
            "/mnt/slow": subprocess.TimeoutExpired("mount", 20),
            "/mnt/nobin": FileNotFoundError("mount"),
        }
    )

    report = lockdown.activate_lockdown(actor="admin", reason="Verdacht", run=run)

    assert [call[0] for call in env.calls] == [
        "cancel",
        "cancel",
        "kill",
        "kill",
        "pause",
        "bump",
    ]
    assert [call[1:] for call in env.calls if call[0] == "cancel"] == [
        ("backup", True),
        ("pbs", True),
    ]
    assert [call[2] for call in env.calls if call[0] == "kill"] == [0, 0]
    assert env.calls[-1] == ("bump", True)
    assert run.attempted == ["/mnt/ok", "/mnt/denied", "/mnt/slow", "/mnt/nobin"]
    assert {entry["mountpoint"]: entry["error"] for entry in report["remount"]} == {
        "/mnt/ok": None,
        "/mnt/denied": "keine Berechtigung (CAP_SYS_ADMIN fehlt)",
        "/mnt/slow": "Zeitüberschreitung nach 20 s",
        "/mnt/nobin": "mount-Programm nicht gefunden",
    }
    assert report["sessions_invalidated"] is True
    assert lockdown.is_locked_down() is True
    assert lockdown.lockdown_state()["reason"] == "Verdacht"
    assert scheduler_control.scheduler_state(env.db)["paused"] is True

    released = lockdown.release_lockdown(actor="admin")

    assert released == {
        "released": True,
        "manual_steps": ["mount -o remount,rw /mnt/ok"],
        "scheduler": {"restored_pause": False},
    }
    assert lockdown.is_locked_down() is False
    assert scheduler_control.scheduler_state(env.db)["paused"] is False
    assert env.db.audit_list(limit=1)[0]["event_type"] == "lockdown_released"


def test_failing_step_does_not_abort_lockdown(env, monkeypatch):
    def broken_kill(graceful_sec, *, scope):
        raise runtime_state.ProcessTerminationError("Exit nicht bestätigt")

    monkeypatch.setattr(runtime_state, "terminate_active_processes", broken_kill)

    report = lockdown.activate_lockdown(actor="admin", reason="", run=_fake_run({}))

    assert report["kill"]["backup"] == {"ok": False, "error": "Exit nicht bestätigt"}
    assert report["scheduler"]["ok"] is True
    assert env.calls[-1] == ("bump", True)
    assert lockdown.is_locked_down() is True


def test_report_caps_mountpoints_and_keeps_successes(env):
    paths = [f"/mnt/{'x' * 80}{index}" for index in range(60)]
    env.pairs.extend(_pair(path) for path in paths)
    succeeded = {paths[0], paths[49]}
    run = _fake_run(
        {
            path: (0, "") if path in succeeded else (32, "permission denied")
            for path in paths
        }
    )

    lockdown.activate_lockdown(actor="admin", reason="Test", run=run)

    stored = lockdown.lockdown_state()["report"]
    assert len(run.attempted) == 50
    assert len(json.dumps(stored, ensure_ascii=False).encode("utf-8")) <= 4096
    assert stored["remount_omitted"] > 0
    assert succeeded <= {entry["mountpoint"] for entry in stored["remount"]}
    steps = lockdown.release_lockdown(actor="admin")["manual_steps"]
    assert sorted(steps) == sorted(f"mount -o remount,rw {path}" for path in succeeded)


def test_route_wrong_password_sets_no_lockdown(env, monkeypatch):
    def deny(request, user, password):
        raise HTTPException(403, {"message": "Aktuelles Passwort falsch"})

    monkeypatch.setattr(api_lockdown, "require_reauthentication", deny)
    body = api_lockdown.LockdownRequest(password="falsch", reason="Test")

    with pytest.raises(HTTPException) as denied:
        api_lockdown.start_lockdown(body, request=None, user="admin")

    assert denied.value.status_code == 403
    assert lockdown.is_locked_down() is False
    assert env.calls == []


def test_route_activates_and_releases_without_leaking_password(
    env, monkeypatch, caplog
):
    seen: list[tuple[str, str]] = []

    def accept(request, user, password):
        seen.append((user, password))
        return "generation"

    monkeypatch.setattr(api_lockdown, "require_reauthentication", accept)
    caplog.set_level(logging.DEBUG)
    body = api_lockdown.LockdownRequest(password=PASSWORD, reason="Panik")
    assert PASSWORD not in repr(body)

    report = api_lockdown.start_lockdown(body, request=None, user="admin")
    assert report["sessions_invalidated"] is True
    assert api_lockdown.get_lockdown()["actor"] == "admin"

    release = api_lockdown.ReleaseRequest(password=PASSWORD)
    released = api_lockdown.end_lockdown(release, request=None, user="admin")
    assert released == {
        "released": True,
        "manual_steps": [],
        "scheduler": {"restored_pause": False},
    }
    assert api_lockdown.end_lockdown(release, request=None, user="admin") == {
        "released": False,
        "manual_steps": [],
    }

    assert seen == [("admin", PASSWORD)] * 3
    audit = json.dumps(env.db.audit_list(limit=50), ensure_ascii=False, default=str)
    assert PASSWORD not in audit
    assert PASSWORD not in caplog.text
