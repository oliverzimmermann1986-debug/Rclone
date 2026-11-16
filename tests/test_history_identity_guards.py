"""Exact-identity history remains bound after pruning and endpoint changes."""

from app import db as db_module
from app.db import Database
from app.jobs.scheduler import rclone_history_key


def test_pruned_history_retains_only_recorded_success_time(tmp_path, monkeypatch):
    database = Database(tmp_path / "history.db")
    key = "rclone:id:photos:fp:current"
    monkeypatch.setattr(db_module.time, "time", lambda: 100.0)
    job = database.job_start("backup")
    monkeypatch.setattr(db_module.time, "time", lambda: 120.0)
    database.job_finish(
        job, "ok", {"pairs": [{"name": "Fotos", "history_key": key, "ok": True}]}
    )
    monkeypatch.setattr(db_module.time, "time", lambda: 200_000.0)
    assert database.jobs_prune(older_than_days=1, keep_latest=0) == 1

    history = database.pair_last_history({key: "Fotos neu"})[key]
    assert history["last_result"] is None
    success = history["last_success"]
    assert success["ended_at"] == 120.0
    assert success["started_at"] is None and success["job_id"] is None
    assert success["history_key"] == key and success["history_pruned"] is True
    assert database.pair_last_history({"rclone:id:photos:fp:replacement": "Fotos neu"})[
        "rclone:id:photos:fp:replacement"
    ] == {"last_result": None, "last_success": None}


def test_retained_failure_does_not_become_success_after_pruning(tmp_path, monkeypatch):
    database = Database(tmp_path / "failed.db")
    key = "rclone:id:photos:fp:failed"
    monkeypatch.setattr(db_module.time, "time", lambda: 100.0)
    job = database.job_start("backup")
    database.job_finish(
        job, "error", {"pairs": [{"name": "Fotos", "history_key": key, "ok": False}]}
    )
    monkeypatch.setattr(db_module.time, "time", lambda: 200_000.0)
    database.jobs_prune(older_than_days=1, keep_latest=0)
    assert database.pair_last_history({key: "Fotos"})[key] == {
        "last_result": None,
        "last_success": None,
    }


def test_endpoint_identity_never_imports_legacy_name_or_id_history(tmp_path):
    database = Database(tmp_path / "endpoints.db")
    original = {
        "id": "1" * 32,
        "name": "Fotos",
        "remote": "cloud:old",
        "local": "/photos",
        "direction": "upload",
        "mode": "copy",
    }
    old_key = rclone_history_key(original)
    changed_key = rclone_history_key({**original, "remote": "cloud:new"})
    renamed_key = rclone_history_key({**original, "name": "Fotos neu"})
    assert changed_key != old_key and renamed_key == old_key
    for history_key in [None, "rclone:id:" + original["id"], old_key]:
        job = database.job_start("backup")
        pair = {"name": "Fotos", "ok": True}
        if history_key is not None:
            pair["history_key"] = history_key
        database.job_finish(job, "ok", {"pairs": [pair]})

    assert database.pair_last_result("Fotos", history_key=changed_key) is None
    assert database.pair_last_success("Fotos", history_key=changed_key) is None
    assert database.pair_baseline_state("Fotos", history_key=changed_key) == "new"
    assert database.pair_last_history({changed_key: "Fotos"})[changed_key] == {
        "last_result": None,
        "last_success": None,
    }
    renamed = database.pair_last_history({renamed_key: "Fotos neu"})[renamed_key]
    assert renamed["last_success"]["history_key"] == old_key
    assert (
        database.pair_baseline_state("Fotos neu", history_key=renamed_key)
        == "succeeded"
    )
