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
