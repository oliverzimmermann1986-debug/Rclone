"""Regression: job_start-Fehler dürfen den Backup-Lock nicht dauerhaft belegen."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import api_jobs


class _FailingDb:
    def job_start(self, *_args, **_kwargs):
        raise RuntimeError("database is locked")


def test_job_start_failure_releases_lock(monkeypatch):
    monkeypatch.setattr(api_jobs, "get_db", lambda: _FailingDb())
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})

    with pytest.raises(HTTPException) as excinfo:
        api_jobs.run_backup(dry_run=True, pairs="Fotos")
    assert excinfo.value.status_code == 500

    # Ohne Fix bliebe der Lock belegt und der zweite Aufruf liefe in 409.
    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()


def test_check_pair_job_start_failure_releases_lock(monkeypatch):
    monkeypatch.setattr(api_jobs, "get_db", lambda: _FailingDb())
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})

    with pytest.raises(HTTPException) as excinfo:
        api_jobs.check_pair("Fotos")
    assert excinfo.value.status_code == 500

    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()


@pytest.mark.parametrize("action", ["check", "restore"])
def test_config_snapshot_failure_does_not_leak_backup_lock(monkeypatch, action):
    class BrokenStore:
        def snapshot_with_revision(self):
            raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(api_jobs, "get_config", lambda: BrokenStore())
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})

    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        if action == "check":
            api_jobs.check_pair("Fotos")
        else:
            api_jobs.start_restore_test(pairs="Fotos")

    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()


def test_setup_failure_after_reservation_finishes_job_and_releases_both_locks(
    monkeypatch,
):
    class BadParallelism:
        def __int__(self):
            raise RuntimeError("invalid parallelism")

    class FakeDb:
        def __init__(self):
            self.finished = []

        def job_start(self, *_args, **_kwargs):
            return 73

        def job_finish(self, job_id, status, summary):
            self.finished.append((job_id, status, summary))
            return True

    class FakeScopeLock:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    database = FakeDb()
    scope_lock = FakeScopeLock()
    snapshot = {
        "backup": {
            "pairs": [
                {
                    "name": "Fotos",
                    "enabled": True,
                    "local": "/data/Fotos",
                    "remote": "cloud:/Fotos",
                }
            ]
        }
    }
    definition = {
        "id": "a" * 32,
        "name": "Fotos täglich",
        "execution_mode": "parallel",
        "max_parallel": BadParallelism(),
    }
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "try_file_lock", lambda _scope: scope_lock)
    monkeypatch.setattr(
        api_jobs, "reconcile_locked_scope", lambda *_args, **_kwargs: {"safe": True}
    )
    monkeypatch.setattr(api_jobs.rclone_job, "reset_cancel", lambda: None)

    with pytest.raises(HTTPException) as caught:
        api_jobs._queue_backup(
            dry_run=False,
            pairs_filter=["Fotos"],
            definition=definition,
            config_snapshot=snapshot,
            config_revision="revision-73",
        )

    assert caught.value.status_code == 500
    assert database.finished == [
        (
            73,
            "error",
            {
                "ok": False,
                "error": "Jobvorbereitung fehlgeschlagen: invalid parallelism",
                "error_code": "setup_failed",
                "stage": "setup",
            },
        )
    ]
    assert scope_lock.releases == 1
    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()
