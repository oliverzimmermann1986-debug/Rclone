from app.routes import api_jobs


def test_run_single_pair_forwards_dry_run(monkeypatch):
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    captured = {}

    def fake_queue_backup(*, dry_run, pairs_filter):
        captured.update(dry_run=dry_run, pairs_filter=pairs_filter)
        return {"ok": True}

    monkeypatch.setattr(api_jobs, "_queue_backup", fake_queue_backup)
    assert api_jobs.run_single_pair("Fotos", dry_run=True) == {"ok": True}
    assert captured == {"dry_run": True, "pairs_filter": ["Fotos"]}


def test_pbs_process_lock_contention_returns_409_without_leaking_web_lock(
    monkeypatch,
):
    from fastapi import HTTPException

    from app.routes import api_pbs

    settings = {
        "enabled": True,
        "targets": [{"name": "Daten", "paths": ["/srv/data"]}],
    }
    monkeypatch.setattr(api_pbs.pbs_backup, "pbs_settings", lambda _cfg=None: settings)
    monkeypatch.setattr(api_pbs.pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    monkeypatch.setattr(
        api_pbs.pbs_backup,
        "pbs_targets",
        lambda _settings: settings["targets"],
    )
    monkeypatch.setattr(api_pbs, "try_file_lock", lambda _scope: None)

    for _ in range(2):
        try:
            api_pbs.pbs_run(api_pbs.PbsRunPayload())
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("process lock contention must be rejected")

    assert api_pbs._lock.locked() is False


def test_pbs_web_worker_persists_prune_failure_as_successful_backup(monkeypatch):
    from app.routes import api_pbs

    summary = {
        "ok": True,
        "backup_ok": True,
        "prune_ok": False,
        "prune_error": "Prune Exit-Code 2",
        "maintenance_failed": True,
        "degraded": True,
        "outcome": "maintenance_failed",
        "trigger": "web",
        "pairs": [
            {
                "name": "pbs:docs",
                "ok": True,
                "backup_ok": True,
                "prune_ok": False,
                "prune_error": "Prune Exit-Code 2",
                "maintenance_failed": True,
            }
        ],
    }

    class Db:
        def __init__(self):
            self.finished = []
            self.audits = []

        def job_get(self, job_id):
            assert job_id == 42
            return {"id": job_id, "status": "running"}

        def job_finish(self, job_id, status, result):
            self.finished.append((job_id, status, result))
            return True

        def audit_add(self, event, *, actor, details):
            self.audits.append((event, actor, details))

    class ScopeLock:
        released = False

        def release(self):
            self.released = True

    database = Db()
    scope_lock = ScopeLock()
    monkeypatch.setattr(api_pbs, "get_db", lambda: database)
    monkeypatch.setattr(
        api_pbs.pbs_backup,
        "run_pbs_backup",
        lambda *_args, **_kwargs: summary,
    )

    assert api_pbs._lock.acquire(blocking=False)
    api_pbs._run_thread(42, ["docs"], {"pbs:docs": "pbs:id:docs"}, scope_lock, {})

    assert database.finished[0][1] == "ok"
    assert database.finished[0][2]["maintenance_failed"] is True
    assert database.audits[0][0:2] == ("pbs_prune_failed", "web")
    assert database.audits[0][2]["backup_ok"] is True
    assert scope_lock.released is True
    assert api_pbs._lock.locked() is False


def test_unsaved_pair_connection_test_uses_inline_draft(tmp_path, monkeypatch):
    import json
    import subprocess

    import bcrypt
    import yaml

    from app import config_store
    from app.config_store import Config
    from app.routes import api_test

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "password_hash": bcrypt.hashpw(
                        b"test-password-long", bcrypt.gensalt(rounds=4)
                    ).decode(),
                    "secret_key": "secret-key-that-is-long-enough-123456789",
                    "local_browse_roots": [str(tmp_path)],
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path / "logs"),
                    "temp_dir": str(tmp_path / "tmp"),
                },
                "backup": {"enabled": True, "pairs": [], "default_schedule": "manual"},
            }
        ),
        encoding="utf-8",
    )
    store = Config(config_path)
    monkeypatch.setattr(config_store, "_config", store)

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "listremotes":
            return subprocess.CompletedProcess(command, 0, "pcloud:\n", "")
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"count": 12, "bytes": 3456}), ""
        )

    monkeypatch.setattr(api_test.subprocess, "run", fake_run)
    draft = {
        "name": "Entwurf",
        "enabled": True,
        "remote": "pcloud:/noch-nicht-gespeichert",
        "local": str(tmp_path),
        "direction": "push",
        "mode": "copy",
        "schedule": "manual",
    }
    result = api_test.test_rclone(api_test.RcloneTest(pair=draft))

    assert result["ok"] is True
    assert result["tested_unsaved"] is True
    assert result["remote_path"] == draft["remote"]
    assert result["local_exists"] is True
    assert result["remote_size"] == {"count": 12, "bytes": 3456}
    assert calls[1][-2:] == ["--", draft["remote"]]


def test_schedule_preview_returns_timezone_aware_next_runs():
    from app.routes import api_config

    result = api_config.preview_schedule(
        api_config.SchedulePreviewRequest(
            expression="0 3 * * *", timezone="Europe/Berlin", count=3
        )
    )

    assert result["ok"] is True
    assert result["enabled"] is True
    assert result["timezone"] == "Europe/Berlin"
    assert len(result["next_runs"]) == 3
    assert all("+" in item["iso"] for item in result["next_runs"])
    assert [item["timestamp"] for item in result["next_runs"]] == sorted(
        item["timestamp"] for item in result["next_runs"]
    )


def test_schedule_preview_supports_manual_and_rejects_invalid_cron():
    from fastapi import HTTPException

    from app.routes import api_config

    manual = api_config.preview_schedule(
        api_config.SchedulePreviewRequest(expression="manual")
    )
    assert manual["enabled"] is False
    assert manual["next_runs"] == []

    try:
        api_config.preview_schedule(
            api_config.SchedulePreviewRequest(expression="not a cron")
        )
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("invalid cron must be rejected")


def test_restore_test_rejects_unknown_pair_without_taking_lock(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    try:
        api_jobs.start_restore_test(pairs="Gibtsnicht")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("unbekanntes Pair muss abgelehnt werden")
    # Der Web-Lock darf beim frühen Abbruch nicht hängen bleiben.
    assert api_jobs._locks["backup"].locked() is False


def test_restore_test_lock_contention_returns_409(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    assert api_jobs._locks["backup"].acquire(blocking=False)
    try:
        api_jobs.start_restore_test(pairs=None)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("belegter Backup-Scope muss 409 liefern")
    finally:
        api_jobs._locks["backup"].release()


def test_cancel_covers_every_backup_scope_kind():
    from app.jobs.job_lifecycle import BACKUP_KINDS

    # Ein laufender Restore-Drill muss abbrechbar sein.
    assert "restoretest" in BACKUP_KINDS


def test_restoretest_is_supported_by_history_filters_and_current_status(monkeypatch):
    running = {
        "id": 42,
        "kind": "restoretest",
        "status": "running",
        "started_at": 123.0,
    }

    class FakeDB:
        def job_running(self, kind):
            return running if kind == "restoretest" else None

        def job_list(self, **kwargs):
            assert kwargs.get("kind") == "restoretest"
            return [running]

        def job_count(self, **kwargs):
            assert kwargs.get("kind") == "restoretest"
            return 1

    monkeypatch.setattr(api_jobs, "get_db", lambda: FakeDB())

    assert api_jobs.list_jobs(kind="restoretest") == [running]
    assert api_jobs.search_jobs(kind="restoretest") == {
        "items": [running],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }
    assert api_jobs.export_jobs_csv(kind="restoretest").status_code == 200
    current = api_jobs.status_current()
    assert current["restoretest"] == running
    assert current["backup"] is None
