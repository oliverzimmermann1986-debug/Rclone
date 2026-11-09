"""Review-Befunde: Lockdown-Push vor dem Session-Bump (#6), PBS-Drossel (#11)
und geteilter Wartungs-Cache der Scheduler-CLI (#15)."""

from __future__ import annotations

import time
from types import SimpleNamespace

from test_integration_hooks_scheduler import _scheduler

from app import auth, lockdown, push_notifications, scheduler_control
from app.db import Database
from app.jobs import rclone_sync, runtime_state, scheduler_cli

DUE = (["docs"], [{"name": "docs", "due": True}])
KEY = "pbs_throttle_since"


def test_lockdown_push_is_sent_synchronously_before_session_bump(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    calls = []
    monkeypatch.setattr(
        lockdown,
        "get_config",
        lambda: SimpleNamespace(get=lambda *_k, default=None: []),
    )
    monkeypatch.setattr(rclone_sync, "cancel_job", lambda scope: {"ok": True})
    monkeypatch.setattr(
        runtime_state, "terminate_active_processes", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(scheduler_control, "pause_scheduler", lambda **_k: {"until": 1})
    monkeypatch.setattr(auth, "bump_session_version", lambda: calls.append("bump"))
    monkeypatch.setattr(
        push_notifications,
        "queue_push_notification",
        lambda *_a, **_k: calls.append("outbox-only") or {"queued": 1},
    )

    def send(event, *_args, db=None, **_kwargs):
        calls.append(("send", event, db is database))
        return {"queued": 3, "sent": 2, "failed": 0, "removed": 0, "retrying": 1}

    monkeypatch.setattr(lockdown, "send_push_notifications", send)

    report = lockdown.activate_lockdown(actor="admin", reason="x", db=database)

    assert calls == [("send", "lockdown", True), "bump"]
    assert report["notify"] == {"ok": True, "delivered": 2, "failed": 1}


def _maintenance(env, monkeypatch, active):
    calls = []

    def check(settings):
        calls.append(settings)
        return {"active": active, "tasks": [{"type": "garbage_collection"}]}

    monkeypatch.setattr(scheduler_cli, "maintenance_active", check)
    return calls


def test_throttle_override_clears_timestamp(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path, lock_free=False)
    monkeypatch.setattr(scheduler_cli, "find_due_pairs", lambda *_a: DUE)
    _maintenance(env, monkeypatch, True)
    env.db.runtime[KEY] = time.time() - 6 * 3600 - 60

    scheduler_cli.main()

    assert env.locks == ["backup"]  # Override: rclone-Phase fragt den Lock an
    assert KEY not in env.db.runtime


def test_no_drill_starts_while_throttle_defers_pairs(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler_cli, "find_due_pairs", lambda *_a: DUE)
    monkeypatch.setattr(scheduler_cli, "restore_test_due", lambda *_a: {"due": True})
    monkeypatch.setattr(scheduler_cli, "bitrot_scrub_due", lambda *_a: {"due": True})
    _maintenance(env, monkeypatch, True)

    assert scheduler_cli.main() == 0
    assert env.locks == [] and env.db.started == []
    assert env.db.runtime[KEY] > 0


def test_timestamp_cleared_after_maintenance_even_without_due_pairs(
    tmp_path, monkeypatch
):
    env = _scheduler(monkeypatch, tmp_path)
    calls = _maintenance(env, monkeypatch, False)
    scheduler_cli.main()
    assert calls == []  # ohne Zeitstempel und fällige Pairs keine PBS-Abfrage

    env.db.runtime[KEY] = time.time() - 60
    scheduler_cli.main()

    assert len(calls) == 1
    assert KEY not in env.db.runtime


def test_maintenance_check_is_shared_between_ticks(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler_cli, "find_due_pairs", lambda *_a: DUE)
    calls = _maintenance(env, monkeypatch, True)

    scheduler_cli.main()
    scheduler_cli.main()  # neuer CLI-Prozess innerhalb poll_seconds (Default 60 s)

    assert len(calls) == 1
    assert env.locks == []  # auch der zweite Tick stellt aus dem Cache zurück
    cached = env.db.runtime["pbs_maintenance_check"]
    cached["checked_at"] = "kaputt"  # DB-Inhalt ist Datum, kein Vertrauen
    scheduler_cli.main()
    assert len(calls) == 2
