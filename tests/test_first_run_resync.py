"""First-Run-Resync: nur erlaubt, solange das Pair nie erfolgreich lief."""

from __future__ import annotations

from app.jobs import rclone_sync


class _Db:
    def __init__(self, last_success):
        self._last = last_success

    def pair_last_success(self, _name):
        return self._last


def test_first_run_allowed_without_prior_success(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db(None))
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is True


def test_blocked_after_first_success(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db({"ended_at": 1.0}))
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is False


def test_opt_out_via_pair_and_global(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db(None))
    assert (
        rclone_sync._first_run_resync_allowed(
            {"auto_resync_first_run": False}, {}, "Serien"
        )
        is False
    )
    assert (
        rclone_sync._first_run_resync_allowed(
            {}, {"auto_resync_first_run": False}, "Serien"
        )
        is False
    )


def test_db_failure_stays_closed(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.db.get_db", boom)
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is False
