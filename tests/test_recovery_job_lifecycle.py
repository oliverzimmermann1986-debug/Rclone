from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.db import Database, JobAlreadyRunningError
from app.jobs import runtime_state
from app.routes import api_recovery as api


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = Database(tmp_path / "jobs.db")
    state = tmp_path / "runtime"
    for name, path in {
        "STATE_DIR": state,
        "RUN_FILE": state / "current-run.json",
        "PROCS_DIR": state / "processes",
        "CANCEL_FILE": state / "cancel.requested",
    }.items():
        monkeypatch.setattr(runtime_state, name, path)
    lease = SimpleNamespace(held=False, releases=0)

    def release():
        lease.held = False
        lease.releases += 1

    def acquire(scope):
        assert scope == "backup"
        if lease.held:
            return None
        lease.held = True
        return lease

    lease.release = release
    monkeypatch.setattr(api, "try_file_lock", acquire)
    monkeypatch.setattr(api, "get_db", lambda: database)
    return database, lease, {"name": "Dummy"}


def queue_worker(background, worker, pair):
    database, job_id, lease = api._start_recovery_job()
    api._queue_recovery(background, database, job_id, lease, worker, {}, pair)
    return job_id


def test_lock_precedes_reservation_and_prevents_scheduler_reconciliation(
    setup, monkeypatch
):
    from app.jobs import scheduler_cli

    database, lease, pair = setup
    original = database.job_start

    def reserve(*args, **kwargs):
        assert lease.held
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "job_start", reserve)
    background = BackgroundTasks()

    def worker(db, config, pair, *, job_id):
        assert lease.held
        db.job_finish(job_id, "ok")

    job_id = queue_worker(background, worker, pair)

    @contextmanager
    def unavailable(scope):
        assert scope != "backup" or lease.held
        yield None

    monkeypatch.setattr(scheduler_cli, "file_lock_or_none", unavailable)
    scheduler_cli._reconcile_available_scopes(database)
    assert database.job_get(job_id)["status"] == "running"
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    assert database.job_get(job_id)["status"] == "ok"
    assert not lease.held and lease.releases == 1


@pytest.mark.parametrize(
    "failure",
    ["get_db", "job_start", "reconcile_locked_scope", "job_terminal_recover_pending"],
)
def test_reservation_failures_release_lock(setup, monkeypatch, failure):
    database, lease, _pair = setup

    def fail(*args, **kwargs):
        raise RuntimeError("dummy setup failure")

    owner = api if failure in {"get_db", "reconcile_locked_scope"} else database
    monkeypatch.setattr(owner, failure, fail)
    with pytest.raises(RuntimeError, match="dummy setup failure"):
        api._start_recovery_job()
    assert not lease.held and lease.releases == 1


def test_lock_conflict_does_not_access_database(setup, monkeypatch):
    _database, lease, _pair = setup
    lease.held = True
    monkeypatch.setattr(api, "get_db", lambda: pytest.fail("DB reached before lock"))
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job()
    assert caught.value.status_code == 409 and lease.releases == 0


def test_reservation_race_releases_lock(setup, monkeypatch):
    database, lease, _pair = setup

    def conflict(*args, **kwargs):
        raise JobAlreadyRunningError("dummy reservation race")

    monkeypatch.setattr(database, "job_start", conflict)
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job()
    assert caught.value.status_code == 409 and not lease.held


def test_unconfirmed_child_blocks_reservation(setup, monkeypatch):
    database, lease, _pair = setup
    monkeypatch.setattr(api, "reconcile_locked_scope", lambda *a, **k: {"safe": False})
    monkeypatch.setattr(
        database, "job_start", lambda *a, **k: pytest.fail("child ignored")
    )
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job()
    assert caught.value.status_code == 409 and not lease.held


@pytest.mark.parametrize("failure", ["audit", "enqueue"])
def test_queue_failure_finishes_reservation_and_releases_lock(
    setup, monkeypatch, failure
):
    database, lease, pair = setup
    background = BackgroundTasks()
    db, job_id, held = api._start_recovery_job()

    def fail(*args, **kwargs):
        raise RuntimeError("dummy queue failure")

    monkeypatch.setattr(
        database if failure == "audit" else background,
        "audit_add" if failure == "audit" else "add_task",
        fail,
    )
    with pytest.raises(RuntimeError, match="dummy queue failure"):
        api._queue_recovery(
            background,
            db,
            job_id,
            held,
            api.run_selective_restore,
            {},
            pair,
            ["a.txt"],
            recovery_point="current",
            max_total_mb=1,
        )
    assert database.job_get(job_id)["status"] == "error"
    assert not lease.held and lease.releases == 1


@pytest.mark.parametrize(
    "failure", ["raise", "unfinished", "db_finish", "after_finish"]
)
def test_worker_failure_releases_lock_and_preserves_confirmed_completion(
    setup, monkeypatch, failure
):
    database, lease, pair = setup
    background = BackgroundTasks()

    def worker(db, config, pair, *, job_id):
        if failure == "after_finish":
            db.job_finish(job_id, "ok")
            raise RuntimeError("dummy reporting failure after completion")
        if failure != "unfinished":
            raise RuntimeError("dummy worker failure")

    job_id = queue_worker(background, worker, pair)
    if failure == "db_finish":
        monkeypatch.setattr(
            database,
            "job_finish_external",
            lambda *a, **k: (_ for _ in ()).throw(OSError("dummy")),
        )
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    assert not lease.held and lease.releases == 1
    assert database.job_get(job_id)["status"] == (
        "running"
        if failure == "db_finish"
        else "ok"
        if failure == "after_finish"
        else "error"
    )
