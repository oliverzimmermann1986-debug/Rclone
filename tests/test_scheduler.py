from datetime import datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from app.jobs import scheduler_cli
from app.jobs.scheduler import (
    find_due_pairs,
    pbs_history_key,
    rclone_history_key,
    restore_test_due,
)


class FakeDb:
    def __init__(self, *, last_success=None, last_attempt=None):
        self.last_success = last_success
        self.last_attempt = last_attempt
        self.bulk_calls = 0

    def pair_last_history(self, identities):
        self.bulk_calls += 1
        return {
            history_key: {
                "last_success": self.last_success,
                "last_result": self.last_attempt,
            }
            for history_key in identities
        }

    def pair_last_success(self, _name):
        raise AssertionError("Scheduler muss die Bulk-Historie verwenden")

    def pair_last_result(self, _name):
        raise AssertionError("Scheduler muss die Bulk-Historie verwenden")


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


def test_restore_test_due_accepts_scheduler_snapshot_mapping():
    snapshot = {
        "backup": {
            "timezone": "Europe/Berlin",
            "scheduler_retry_minutes": 60,
            "scheduler_grace_minutes": 15,
            "restore_test": {"enabled": True, "schedule": "0 3 * * *"},
        }
    }
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()

    result = restore_test_due(snapshot, FakeDb(), now=now)

    assert result["due"] is False
    assert result["reason"] == "waiting_for_first_schedule"


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
    assert db.bulk_calls == 1


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
    assert db.bulk_calls == 1


def test_running_attempt_from_crashed_scheduler_obeys_backoff():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    attempt = {
        "ok": False,
        "status": "running",
        "started_at": now - 30 * 60,
        "pair": {
            "name": "Fotos",
            "pending": True,
            "trigger": "scheduler",
        },
    }
    due, status = find_due_pairs(_config(), FakeDb(last_attempt=attempt), now=now)
    assert due == []
    assert status[0]["reason"] == "retry_backoff"


def test_scheduler_rejects_non_five_field_cron():
    cfg = _config()
    cfg["backup"]["pairs"][0]["schedule"] = "0 0 3 * * *"
    due, status = find_due_pairs(
        cfg,
        FakeDb(),
        now=datetime(2026, 7, 10, 3, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp(),
    )
    assert due == []
    assert status[0]["reason"] == "invalid_schedule"


def test_history_keys_are_typed_and_survive_rename_with_same_identity():
    first = {
        "name": "Fotos",
        "remote": "cloud:/photos",
        "local": "/srv/photos",
        "direction": "pull",
        "mode": "copy",
    }
    renamed = {**first, "name": "Urlaub"}
    assert rclone_history_key(first) == rclone_history_key(renamed)
    assert rclone_history_key(first).startswith("rclone:")
    assert pbs_history_key(
        {"repository": "user@pbs:store"},
        {"name": "docs", "paths": ["/srv/docs"]},
    ).startswith("pbs:")
    assert rclone_history_key(first) != pbs_history_key(
        {"repository": "user@pbs:store"},
        {"name": "docs", "paths": ["/srv/docs"]},
    )


def test_fall_back_local_cron_slot_runs_only_once():
    cfg = _config()
    cfg["backup"]["pairs"][0]["schedule"] = "30 2 * * *"
    timezone = ZoneInfo("Europe/Berlin")
    first_occurrence = datetime(
        2026, 10, 25, 2, 30, 5, tzinfo=timezone, fold=0
    ).timestamp()
    second_occurrence = datetime(
        2026, 10, 25, 2, 30, 5, tzinfo=timezone, fold=1
    ).timestamp()

    first_db = FakeDb()
    due, status = find_due_pairs(cfg, first_db, now=first_occurrence)
    assert due == ["Fotos"]
    slot = status[0]["scheduled_slot"]
    assert slot.endswith("2026-10-25T02:30")

    completed_at = first_occurrence + 10
    completed = {
        "ok": True,
        "started_at": first_occurrence,
        "ended_at": completed_at,
        "job_id": 7,
        "pair": {
            "name": "Fotos",
            "ok": True,
            "trigger": "scheduler",
            "scheduled_slot": slot,
        },
    }
    second_db = FakeDb(last_success=completed, last_attempt=completed)
    due, status = find_due_pairs(cfg, second_db, now=second_occurrence)
    assert due == []
    assert status[0]["reason"] == "slot_already_attempted"

    next_day = datetime(2026, 10, 26, 2, 30, 5, tzinfo=timezone).timestamp()
    due, status = find_due_pairs(cfg, second_db, now=next_day)
    assert due == ["Fotos"]
    assert status[0]["scheduled_slot"].endswith("2026-10-26T02:30")


def test_fall_back_does_not_turn_failed_first_fold_into_second_cron_run():
    cfg = _config()
    cfg["backup"]["pairs"][0]["schedule"] = "30 2 * * *"
    timezone = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 10, 25, 2, 30, 5, tzinfo=timezone, fold=0).timestamp()
    second = datetime(2026, 10, 25, 2, 30, 5, tzinfo=timezone, fold=1).timestamp()
    _, first_status = find_due_pairs(cfg, FakeDb(), now=first)
    slot = first_status[0]["scheduled_slot"]
    prior_success_at = datetime(2026, 10, 24, 2, 31, tzinfo=timezone).timestamp()
    prior_success = {
        "ok": True,
        "ended_at": prior_success_at,
        "pair": {"trigger": "scheduler"},
    }
    failed = {
        "ok": False,
        "status": "error",
        "started_at": first,
        "ended_at": first,
        "job_id": 8,
        "pair": {
            "trigger": "scheduler",
            "scheduled_slot": slot,
        },
    }
    database = FakeDb(last_success=prior_success, last_attempt=failed)

    due, status = find_due_pairs(cfg, database, now=second)
    assert due == []
    assert status[0]["reason"] == "slot_already_attempted"

    due, status = find_due_pairs(cfg, database, now=second + 65)
    assert due == ["Fotos"]
    assert status[0]["reason"] == "retry_after_failure"


def test_disabled_rclone_backup_does_not_disable_pbs_scheduler(tmp_path, monkeypatch):
    class Config:
        data = {
            "backup": {"enabled": False, "pairs": []},
            "paths": {"logs_dir": str(tmp_path)},
        }

        def get(self, *keys, default=None):
            value = self.data
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return value

    class Db:
        def __init__(self):
            self.started = []
            self.finished = []

        def job_start(self, kind, **kwargs):
            self.started.append((kind, kwargs))
            return 42

        def job_finish(self, job_id, status, summary):
            self.finished.append((job_id, status, summary))
            return True

        def jobs_mark_all_running_stale(self, **_kwargs):
            return 0

    @contextmanager
    def lock(_name):
        yield object()

    database = Db()
    monkeypatch.setattr(scheduler_cli, "get_config", Config)
    monkeypatch.setattr(scheduler_cli, "get_db", lambda: database)
    monkeypatch.setattr(scheduler_cli, "_configure_logging", lambda _path=None: None)
    monkeypatch.setattr(scheduler_cli, "scheduler_state", lambda _db: {"paused": False})
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rclone scheduler must stay disabled")
        ),
    )
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pbs_targets",
        lambda *_args, **_kwargs: (
            ["docs"],
            [
                {
                    "name": "docs",
                    "run_name": "pbs:docs",
                    "history_key": "pbs:id:docs",
                    "scheduled_slot": "slot",
                    "due": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(scheduler_cli, "file_lock_or_none", lock)
    monkeypatch.setattr(
        scheduler_cli,
        "run_pbs_backup",
        lambda *_args, **_kwargs: {
            "ok": True,
            "pairs": [{"name": "pbs:docs", "ok": True}],
        },
    )

    assert scheduler_cli.main() == 0
    assert database.started[0][0] == "pbs"
    assert database.started[0][1]["attempts"][0]["name"] == "pbs:docs"
    assert database.finished[0][1] == "ok"


def test_scheduler_rechecks_due_state_after_scope_lock(tmp_path, monkeypatch):
    class Config:
        data = {
            "backup": {"enabled": True, "pairs": [{"name": "Fotos"}]},
            "paths": {"logs_dir": str(tmp_path)},
        }

        def get(self, *keys, default=None):
            value = self.data
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return value

    class Db:
        def jobs_mark_all_running_stale(self, **_kwargs):
            return 0

    @contextmanager
    def lock(_name):
        yield object()

    due_results = iter(
        (
            (
                ["Fotos"],
                [
                    {
                        "name": "Fotos",
                        "history_key": "rclone:id:photos",
                        "scheduled_slot": "slot",
                        "due": True,
                    }
                ],
            ),
            ([], []),
        )
    )
    monkeypatch.setattr(scheduler_cli, "get_config", Config)
    monkeypatch.setattr(scheduler_cli, "get_db", Db)
    monkeypatch.setattr(scheduler_cli, "_configure_logging", lambda _path=None: None)
    monkeypatch.setattr(scheduler_cli, "scheduler_state", lambda _db: {"paused": False})
    monkeypatch.setattr(
        scheduler_cli, "find_due_pairs", lambda *_args, **_kwargs: next(due_results)
    )
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pbs_targets",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(scheduler_cli, "file_lock_or_none", lock)
    monkeypatch.setattr(
        scheduler_cli,
        "reconcile_locked_scope",
        lambda *_args, **_kwargs: {"safe": True, "recovered_jobs": 0},
    )
    monkeypatch.setattr(
        scheduler_cli,
        "run_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("überholter Scheduler-Slot darf nicht gestartet werden")
        ),
    )

    assert scheduler_cli.main() == 0


def test_cancelled_pair_makes_cli_job_cancelled():
    assert (
        scheduler_cli._job_status(
            {"ok": False, "pairs": [{"name": "pbs:docs", "cancelled": True}]}
        )
        == "cancelled"
    )
