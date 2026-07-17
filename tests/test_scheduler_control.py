import time

from app.db import Database
from app.scheduler_control import pause_scheduler, resume_scheduler, scheduler_state


def test_scheduler_pause_persists_and_expires(tmp_path):
    database = Database(tmp_path / "app.db")
    state = pause_scheduler(
        seconds=3600, reason="Proxmox Backup", actor="admin", db=database
    )
    assert state["paused"] is True
    assert state["reason"] == "Proxmox Backup"
    assert state["remaining_seconds"] > 3500

    reopened = Database(tmp_path / "app.db")
    persisted = scheduler_state(reopened)
    assert persisted["paused"] is True
    assert persisted["actor"] == "admin"

    expired = scheduler_state(reopened, now=float(persisted["until"]) + 1)
    assert expired["paused"] is False


def test_scheduler_resume_is_audited(tmp_path):
    database = Database(tmp_path / "app.db")
    pause_scheduler(seconds=600, reason="NAS restart", actor="admin", db=database)
    resumed = resume_scheduler(actor="admin", db=database)
    assert resumed["paused"] is False
    events = database.audit_list(limit=10)
    assert [event["event_type"] for event in events[:2]] == [
        "scheduler_resumed",
        "scheduler_paused",
    ]
    assert events[0]["details"]["reason"] == "NAS restart"


def test_scheduler_pause_rejects_past_and_excessive_until(tmp_path):
    database = Database(tmp_path / "app.db")
    now = time.time()
    for until in (now - 1, now + 32 * 86400):
        try:
            pause_scheduler(until=until, db=database)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid pause accepted")
