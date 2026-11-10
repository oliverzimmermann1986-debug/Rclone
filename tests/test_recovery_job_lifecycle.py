from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.db import Database
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
    pair = {"name": "Dummy"}
    return database, lease, pair


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

    job_id = api._queue_recovery(background, worker, {}, pair)
    state = runtime_state.load_run_state()
    assert state["job_id"] == job_id and state["kind"] == "recovery"
    assert state["status"] == "running"

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
    assert runtime_state.load_run_state()["status"] == "ok"
    assert not lease.held and lease.releases == 1


@pytest.mark.parametrize(
    "failure", ["get_db", "job_start", "active_processes", "begin_run"]
)
def test_reservation_failures_release_lock(setup, monkeypatch, failure):
    database, lease, pair = setup

    def fail(*args, **kwargs):
        raise RuntimeError("dummy setup failure")

    owner = (
        api
        if failure == "get_db"
        else database
        if failure == "job_start"
        else runtime_state
    )
    monkeypatch.setattr(owner, failure, fail)
    with pytest.raises(RuntimeError, match="dummy setup failure"):
        api._start_recovery_job(pair)
    assert not lease.held and lease.releases == 1
    if failure == "begin_run":
        assert database.job_get(1)["status"] == "error"


def test_lock_conflict_does_not_access_database(setup, monkeypatch):
    _database, lease, pair = setup
    lease.held = True
    monkeypatch.setattr(api, "get_db", lambda: pytest.fail("DB reached before lock"))
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job(pair)
    assert caught.value.status_code == 409 and lease.releases == 0


def test_database_conflict_releases_lock(setup):
    database, lease, pair = setup
    job_id = database.job_start("backup", exclusive_scope=True)
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job(pair)
    assert caught.value.status_code == 409
    assert database.job_get(job_id)["status"] == "running"
    assert not lease.held


def test_orphan_child_blocks_reservation(setup, monkeypatch):
    database, lease, pair = setup
    monkeypatch.setattr(runtime_state, "active_processes", lambda scope: [{"pid": 123}])
    monkeypatch.setattr(
        database, "job_start", lambda *a, **k: pytest.fail("child ignored")
    )
    with pytest.raises(HTTPException) as caught:
        api._start_recovery_job(pair)
    assert caught.value.status_code == 409 and not lease.held


@pytest.mark.parametrize("failure", ["audit", "enqueue"])
def test_setup_after_reservation_finishes_job_and_runtime(setup, monkeypatch, failure):
    database, lease, pair = setup

    def fail(*args, **kwargs):
        raise RuntimeError("dummy queue failure")

    background = BackgroundTasks()
    if failure == "audit":
        monkeypatch.setattr(database, "audit_add", fail)
    else:
        monkeypatch.setattr(background, "add_task", fail)
    with pytest.raises(RuntimeError, match="dummy queue failure"):
        api._queue_recovery(
            background,
            api.run_selective_restore,
            {},
            pair,
            ["a.txt"],
            recovery_point="current",
            max_total_mb=1,
        )
    assert database.job_get(1)["status"] == "error"
    assert runtime_state.load_run_state()["status"] == "error"
    assert not lease.held


@pytest.mark.parametrize(
    "failure", ["raise", "unfinished", "runtime_finish", "db_finish", "after_finish"]
)
def test_background_failures_finish_and_release(setup, monkeypatch, failure):
    database, lease, pair = setup
    background = BackgroundTasks()

    def worker(db, config, pair, *, job_id):
        if failure in {"runtime_finish", "after_finish"}:
            db.job_finish(job_id, "ok")
            if failure == "after_finish":
                raise RuntimeError("dummy reporting failure after completion")
            return
        if failure != "unfinished":
            raise RuntimeError("dummy worker failure")

    job_id = api._queue_recovery(background, worker, {}, pair)
    if failure == "runtime_finish":
        monkeypatch.setattr(
            runtime_state,
            "finish_run",
            lambda *a, **k: (_ for _ in ()).throw(OSError("dummy")),
        )
    if failure == "db_finish":
        monkeypatch.setattr(
            database,
            "job_finish",
            lambda *a, **k: (_ for _ in ()).throw(OSError("dummy")),
        )
    task = background.tasks[0]
    if failure == "runtime_finish":
        task.func(*task.args, **task.kwargs)
    else:
        with pytest.raises(RuntimeError):
            task.func(*task.args, **task.kwargs)
    assert not lease.held and lease.releases == 1
    if failure != "db_finish":
        assert database.job_get(job_id)["status"] == (
            "ok" if failure in {"runtime_finish", "after_finish"} else "error"
        )
    if failure != "runtime_finish":
        assert runtime_state.load_run_state()["status"] == (
            "ok" if failure == "after_finish" else "error"
        )
