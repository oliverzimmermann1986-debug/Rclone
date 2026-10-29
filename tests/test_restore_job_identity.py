"""Both production entry points supply the durable restore notification identity."""

from contextlib import contextmanager
from types import SimpleNamespace

from app.db import Database
from app.jobs import scheduler_cli
from app.jobs import restore_test as drill
from app.routes import api_jobs


def _result(kwargs):
    return {
        "kind": "restoretest",
        "ok": True,
        "job_id": kwargs["job_id"],
        "trigger": kwargs["trigger"],
        "pairs": [
            {"name": "Fotos", "ok": True},
            {"name": drill.AGGREGATE_RUN_NAME, "ok": True},
        ],
        "history_keys": {
            drill.AGGREGATE_RUN_NAME: drill.HISTORY_KEY
            if kwargs["trigger"] == "scheduler"
            else drill.MANUAL_HISTORY_KEY
        },
    }


def test_manual_api_passes_reserved_job_id_into_restore_run(tmp_path, monkeypatch):
    database = Database(tmp_path / "manual-restore.db")
    job_id = database.job_start("restoretest")
    scope_lock = SimpleNamespace(release=lambda: None)
    snapshot = {"backup": {"pairs": [{"name": "Fotos"}]}}
    cfg = SimpleNamespace(snapshot_with_revision=lambda: (snapshot, "revision"))
    monkeypatch.setattr(api_jobs, "get_config", lambda: cfg)
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    monkeypatch.setattr(
        api_jobs, "_reserve_backup_job", lambda *_a, **_kw: (job_id, scope_lock)
    )
    monkeypatch.setattr(api_jobs, "_audit_best_effort", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        api_jobs, "_setup_job_logger", lambda *_a: (tmp_path / "job.log", None)
    )
    monkeypatch.setattr(api_jobs, "_start_thread", lambda target, **_kw: target())
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return _result(kwargs)

    monkeypatch.setattr(drill, "run_restore_test", run)

    response = api_jobs.start_restore_test(pairs="Fotos")

    assert response["job_id"] == job_id
    assert calls[0]["job_id"] == job_id
    assert calls[0]["pairs_filter"] == ["Fotos"]
    assert calls[0]["trigger"] == "manual"
    assert database.job_get(job_id)["summary"]["job_id"] == job_id
    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()


def test_scheduler_passes_reserved_job_id_into_restore_run(tmp_path, monkeypatch):
    database = Database(tmp_path / "scheduled-restore.db")
    snapshot = {
        "backup": {"enabled": True, "pairs": [{"name": "Fotos"}]},
        "paths": {"logs_dir": str(tmp_path / "logs")},
    }
    cfg = SimpleNamespace(snapshot_with_revision=lambda: (snapshot, "revision"))
    monkeypatch.setattr(scheduler_cli, "get_config", lambda: cfg)
    monkeypatch.setattr(scheduler_cli, "get_db", lambda: database)
    monkeypatch.setattr(scheduler_cli, "_configure_logging", lambda *_a: None)
    monkeypatch.setattr(scheduler_cli, "_reconcile_available_scopes", lambda *_a: None)
    monkeypatch.setattr(scheduler_cli, "check_overdue", lambda *_a: [])
    monkeypatch.setattr(scheduler_cli, "scheduler_state", lambda *_a: {"paused": False})
    monkeypatch.setattr(scheduler_cli, "find_due_pairs", lambda *_a: ([], []))
    monkeypatch.setattr(scheduler_cli, "find_due_pbs_targets", lambda *_a: ([], []))
    monkeypatch.setattr(
        scheduler_cli,
        "restore_test_due",
        lambda *_a: {"due": True, "scheduled_slot": "slot"},
    )
    monkeypatch.setattr(
        scheduler_cli, "reconcile_locked_scope", lambda *_a, **_kw: {"safe": True}
    )
    monkeypatch.setattr(scheduler_cli, "reset_cancel", lambda: None)
    monkeypatch.setattr(
        "app.push_notifications.dispatch_pending_pushes", lambda **_kw: {}
    )

    @contextmanager
    def lock(_scope):
        yield object()

    monkeypatch.setattr(scheduler_cli, "file_lock_or_none", lock)
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return _result(kwargs)

    monkeypatch.setattr(scheduler_cli, "run_restore_test", run)

    assert scheduler_cli.main() == 0

    job = database.job_list(kind="restoretest")[0]
    assert calls[0]["job_id"] == job["id"]
    assert calls[0]["trigger"] == "scheduler"
    assert job["summary"]["job_id"] == job["id"]
    assert job["status"] == "ok"
