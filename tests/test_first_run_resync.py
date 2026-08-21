"""First-Run-Resync: nur erlaubt, solange das Pair nie erfolgreich lief."""

from __future__ import annotations

from app.jobs import rclone_sync


class _Db:
    def __init__(self, state):
        self._state = state
        self.history_key = None

    def pair_baseline_state(self, _name, *, history_key=None):
        self.history_key = history_key
        return self._state


def test_first_run_allowed_without_prior_success(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db("new"))
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is True


def test_blocked_after_first_success(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db("succeeded"))
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is False


def test_first_run_lookup_uses_stable_history_key(monkeypatch):
    database = _Db("succeeded")
    monkeypatch.setattr("app.db.get_db", lambda: database)
    pair = {"id": "0123456789abcdef0123456789abcdef"}

    assert rclone_sync._first_run_resync_allowed(pair, {}, "Umbenannt") is False
    assert database.history_key == "rclone:id:0123456789abcdef0123456789abcdef"


def test_opt_out_via_pair_and_global(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db("new"))
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


def test_ambiguous_history_stays_closed(monkeypatch):
    monkeypatch.setattr("app.db.get_db", lambda: _Db("ambiguous"))
    assert rclone_sync._first_run_resync_allowed({}, {}, "Serien") is False
