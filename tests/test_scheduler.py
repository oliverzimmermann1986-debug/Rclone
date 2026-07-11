from datetime import datetime
from zoneinfo import ZoneInfo

from app.jobs.scheduler import find_due_pairs


class FakeDb:
    def __init__(self, *, last_success=None, last_attempt=None):
        self.last_success = last_success
        self.last_attempt = last_attempt

    def pair_last_success(self, _name):
        return self.last_success

    def pair_last_result(self, _name):
        return self.last_attempt


def _config():
    return {
        "backup": {
            "timezone": "Europe/Berlin",
            "scheduler_retry_minutes": 60,
            "scheduler_grace_minutes": 15,
            "run_on_first_tick": False,
            "pairs": [
                {
                    "name": "Fotos",
                    "enabled": True,
                    "schedule": "0 3 * * *",
                }
            ],
        }
    }


def test_scheduled_first_failure_retries_after_backoff():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    attempt = now - 61 * 60
    db = FakeDb(
        last_attempt={
            "ok": False,
            "ended_at": attempt,
            "pair": {"name": "Fotos", "trigger": "scheduler"},
        }
    )
    due, status = find_due_pairs(_config(), db, now=now)
    assert due == ["Fotos"]
    assert status[0]["reason"] == "retry_after_failure"


def test_manual_failure_does_not_create_scheduler_retry():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    db = FakeDb(
        last_attempt={
            "ok": False,
            "ended_at": now - 61 * 60,
            "pair": {"name": "Fotos", "trigger": "web"},
        }
    )
    due, status = find_due_pairs(_config(), db, now=now)
    assert due == []
    assert status[0]["reason"] == "waiting_for_first_schedule"
