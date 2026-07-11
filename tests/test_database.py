import json
import sqlite3
import time
from pathlib import Path

from app.db import Database


def test_database_backfills_and_indexes_pair_history(tmp_path: Path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at REAL NOT NULL,
            ended_at REAL,
            summary_json TEXT,
            log_file TEXT
        );
    """)
    now = time.time()
    summary = {"pairs": [{"name": "Fotos", "ok": True, "transferred": "1 GiB"}]}
    connection.execute(
        "INSERT INTO jobs(kind,status,started_at,ended_at,summary_json) VALUES('backup','ok',?,?,?)",
        (now - 5, now, json.dumps(summary)),
    )
    connection.commit()
    connection.close()

    db = Database(path)
    result = db.pair_last_success("Fotos")
    assert result is not None
    assert result["pair"]["transferred"] == "1 GiB"
    assert db.stats()["pair_runs"] == 1
    assert db.integrity_check()["ok"] is True


def test_persistent_login_backoff(tmp_path: Path):
    db = Database(tmp_path / "auth.db")
    now = 1000.0
    assert (
        db.auth_record_failure(
            "client", window_sec=300, max_failures=2, lock_sec=60, now=now
        )
        == 0
    )
    assert (
        db.auth_record_failure(
            "client", window_sec=300, max_failures=2, lock_sec=60, now=now + 1
        )
        >= 59
    )
    assert db.auth_retry_after("client", now=now + 2) >= 58
    db.auth_clear("client")
    assert db.auth_retry_after("client", now=now + 2) == 0


def test_mark_all_running_jobs_and_pair_rows_stale(tmp_path: Path):
    db = Database(tmp_path / "stale.db")
    job_id = db.job_start("backup")
    db.job_finish(job_id, "running", {"pairs": [{"name": "Fotos", "ok": False}]})

    assert db.jobs_mark_all_running_stale(reason="test recovery") == 1
    job = db.job_get(job_id)
    assert job["status"] == "stale"
    pair = db.pair_last_result("Fotos")
    assert pair is not None
    assert pair["status"] == "stale"


def test_job_filters_pagination_and_statistics(tmp_path: Path):
    database = Database(tmp_path / "filters.db")
    first = database.job_start("backup")
    database.job_finish(first, "ok", {"pairs": []})
    second = database.job_start("check")
    database.job_finish(second, "error", {"error": "boom"})
    third = database.job_start("backup")
    database.job_finish(third, "ok", {"pairs": []})

    backups = database.job_list(kind="backup", limit=10)
    assert [job["id"] for job in backups] == [third, first]
    assert database.job_list(status="error", limit=10)[0]["id"] == second
    assert database.job_list(kind="backup", limit=1, offset=1)[0]["id"] == first
    assert database.job_count(kind="backup", status="ok") == 2
    stats = database.job_statistics(since=0)
    assert stats["total"] == 3
    assert stats["by_status"] == {"error": 1, "ok": 2}
    assert stats["by_kind"] == {"backup": 2, "check": 1}


def test_pair_last_results_and_successes_are_batched(tmp_path: Path):
    database = Database(tmp_path / "pair-batch.db")
    first = database.job_start("backup")
    database.job_finish(
        first,
        "ok",
        {"pairs": [{"name": "Fotos", "ok": True}, {"name": "Videos", "ok": True}]},
    )
    second = database.job_start("backup")
    database.job_finish(
        second,
        "error",
        {"pairs": [{"name": "Fotos", "ok": False, "error": "remote down"}]},
    )

    latest = database.pair_last_results()
    successes = database.pair_last_successes()
    assert latest["Fotos"]["job_id"] == second
    assert latest["Fotos"]["status"] == "error"
    assert latest["Videos"]["job_id"] == first
    assert successes["Fotos"]["job_id"] == first
    assert successes["Videos"]["job_id"] == first


def test_job_search_matches_summary_log_and_exact_id(tmp_path: Path):
    database = Database(tmp_path / "search.db")
    photos = database.job_start("backup")
    database.job_set_log_file(photos, "/var/log/rclone/fotos-nightly.log")
    database.job_finish(
        photos,
        "error",
        {"pairs": [{"name": "Urlaubsfotos", "ok": False}], "error": "Remote offline"},
    )
    videos = database.job_start("backup")
    database.job_finish(videos, "ok", {"pairs": [{"name": "Videos", "ok": True}]})
    literal = database.job_start("check")
    database.job_finish(literal, "error", {"error": "100% fehlgeschlagen"})

    assert [job["id"] for job in database.job_list(query="urlaubsfotos")] == [photos]
    assert [job["id"] for job in database.job_list(query="fotos-nightly")] == [photos]
    assert [job["id"] for job in database.job_list(query=str(videos))] == [videos]
    assert [job["id"] for job in database.job_list(query="100%")] == [literal]
    assert database.job_count(query="remote offline") == 1
    assert database.job_count(kind="backup", status="error", query="fotos") == 1
