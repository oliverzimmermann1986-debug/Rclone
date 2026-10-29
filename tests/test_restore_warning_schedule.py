from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.jobs.scheduler import RESTORE_TEST_HISTORY_KEY, restore_test_due


def stamp(day, hour, minute=0):
    return datetime(
        2026, 9, day, hour, minute, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()


def config():
    return {
        "backup": {
            "timezone": "Europe/Berlin",
            "scheduler_retry_minutes": 60,
            "restore_test": {"enabled": True, "schedule": "0 3 * * *"},
        }
    }


def partial_summary():
    return {
        "kind": "restoretest",
        "ok": False,
        "trigger": "scheduler",
        "pairs": [
            {
                "name": "Fotos",
                "ok": False,
                "sample_status": "partial_selection",
                "requested_sample_size": 20,
                "sample_size": 19,
                "verified": 19,
                "restored_files": 19,
                "return_code": 0,
                "sample_shortfall_reason": "byte_budget",
                "error": "Teil-Stichprobe: 19 von 20 angeforderten Dateien ausgewählt und erfolgreich geprüft",
            },
            {"name": "Rezepte", "ok": True},
            {"name": "restore-drill", "ok": False, "pairs_tested": 2},
        ],
    }


class HistoryDb:
    def __init__(self, *, status="warning", trigger="scheduler", last_success=None):
        self.last_attempt = {
            "job_id": 81,
            "ok": False,
            "status": status,
            "started_at": stamp(9, 3),
            "ended_at": stamp(9, 3, 5),
            "pair": {"name": "restore-drill", "trigger": trigger},
            "scheduled_slot": "v1|Europe/Berlin|0 3 * * *|2026-09-09T03:00",
        }
        self.history = {"last_result": self.last_attempt, "last_success": last_success}
        self.job = {
            "kind": "restoretest",
            "status": status,
            "summary": partial_summary(),
        }
        self.job["summary"]["trigger"] = trigger
        self.job_calls = []

    def pair_last_history(self, identities):
        assert identities == {RESTORE_TEST_HISTORY_KEY: RESTORE_TEST_HISTORY_KEY}
        return {RESTORE_TEST_HISTORY_KEY: self.history}

    def job_get(self, job_id):
        self.job_calls.append(job_id)
        return self.job


@pytest.mark.parametrize("status", ["warning", "error"])
@pytest.mark.parametrize(
    "previous_success", [None, {"ok": True, "ended_at": stamp(8, 3, 5)}]
)
def test_verified_partial_completes_slot_without_retry_or_history_rewrite(
    status, previous_success
):
    db = HistoryDb(status=status, last_success=previous_success)
    before = deepcopy(db.history)

    result = restore_test_due(config(), db, now=stamp(9, 4, 6))

    assert result["due"] is False
    assert result["reason"] == "partial_restore_slot_completed"
    assert result["last_run"] == (previous_success or {}).get("ended_at")
    assert result["last_scheduled_completion"] == stamp(9, 3, 5)
    assert result["next_run"] == stamp(10, 3)
    assert db.history == before
    assert db.job_calls == [81]


def test_next_regular_restore_slot_is_still_due_after_partial_warning():
    db = HistoryDb()

    result = restore_test_due(config(), db, now=stamp(10, 3, 1))

    assert result["due"] is True
    assert result["scheduled_at"] == stamp(10, 3)
    assert result["scheduled_slot"].endswith("2026-09-10T03:00")
    assert not result["scheduled_slot"].startswith("retry|")
    assert result["last_run"] is None


@pytest.mark.parametrize(
    "defect", ["mismatch", "cancelled", "cleanup", "mixed", "wrong_kind"]
)
def test_actual_restore_failures_keep_retry_backoff(defect):
    db = HistoryDb(status="error")
    summary = db.job["summary"]
    if defect == "mismatch":
        summary["pairs"][0]["verified"] = 18
    elif defect == "cancelled":
        summary["cancelled"] = True
    elif defect == "cleanup":
        summary["pairs"][0]["temp_cleanup_failed"] = True
    elif defect == "mixed":
        summary["pairs"][1].update(ok=False, error="copy failed")
    else:
        db.job["kind"] = "backup"

    before_retry = restore_test_due(config(), db, now=stamp(9, 4, 4))
    retry = restore_test_due(config(), db, now=stamp(9, 4, 6))

    assert before_retry["due"] is False
    assert before_retry["reason"] == "retry_backoff"
    assert retry["due"] is True
    assert retry["reason"] == "retry_after_failure"
    assert "last_scheduled_completion" not in retry


def test_manual_partial_does_not_shift_automatic_schedule():
    db = HistoryDb(
        trigger="manual", last_success={"ok": True, "ended_at": stamp(8, 3, 5)}
    )
    db.last_attempt.update(started_at=stamp(9, 2, 50), ended_at=stamp(9, 2, 55))
    db.last_attempt.pop("scheduled_slot")

    result = restore_test_due(config(), db, now=stamp(9, 3, 1))

    assert result["due"] is True
    assert result["scheduled_at"] == stamp(9, 3)
    assert result["last_run"] == stamp(8, 3, 5)
    assert "last_scheduled_completion" not in result
    assert db.job_calls == []


@pytest.mark.parametrize(
    "adapter", ["missing", "raises", "missing_job", "malformed_summary"]
)
def test_adapter_lookup_failure_never_promotes_unproven_warning(adapter):
    db = HistoryDb()
    if adapter == "missing":
        db.job_get = None
    elif adapter == "raises":

        def raises(_job_id):
            raise RuntimeError("unavailable")

        db.job_get = raises
    elif adapter == "missing_job":
        db.job = None
    else:
        db.job["summary"] = "invalid"

    result = restore_test_due(config(), db, now=stamp(9, 4, 6))

    assert result["due"] is True
    assert result["reason"] == "retry_after_failure"
    assert "last_scheduled_completion" not in result


def test_full_embedded_summary_supports_older_adapter_without_extra_lookup():
    db = HistoryDb(status="error")
    db.last_attempt["summary"] = partial_summary()
    db.job_get = None

    assert restore_test_due(config(), db, now=stamp(9, 4, 6))["due"] is False


def test_persisted_aggregate_warning_does_not_award_success(tmp_path, monkeypatch):
    db = Database(tmp_path / "restore-warning.db")
    monkeypatch.setattr("app.db.time.time", lambda: stamp(9, 3))
    job_id = db.job_start("restoretest")
    summary = partial_summary()
    summary["history_keys"] = {"restore-drill": RESTORE_TEST_HISTORY_KEY}
    monkeypatch.setattr("app.db.time.time", lambda: stamp(9, 3, 5))
    db.job_finish(job_id, "warning", summary)

    result = restore_test_due(config(), db, now=stamp(9, 4, 6))
    stored = db.pair_last_history({RESTORE_TEST_HISTORY_KEY: RESTORE_TEST_HISTORY_KEY})

    assert result["due"] is False
    assert stored[RESTORE_TEST_HISTORY_KEY]["last_success"] is None
    assert stored[RESTORE_TEST_HISTORY_KEY]["last_result"]["ok"] is False
    assert stored[RESTORE_TEST_HISTORY_KEY]["last_result"]["status"] == "warning"
