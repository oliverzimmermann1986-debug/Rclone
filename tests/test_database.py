import json
import sqlite3
import time
from pathlib import Path

import pytest

from app import db as db_module
from app.db import Database, JobAlreadyRunningError


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
    with db.conn() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(jobs)").fetchall()
        }
    assert "idx_jobs_started" in indexes


def test_database_upgrades_old_pair_schema_without_data_loss(tmp_path: Path):
    path = tmp_path / "old-pairs.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at REAL NOT NULL,
            ended_at REAL,
            summary_json TEXT,
            log_file TEXT
        );
        CREATE TABLE pair_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            pair_name TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            result_json TEXT,
            UNIQUE(job_id, pair_name)
        );
        """
    )
    connection.execute(
        "INSERT INTO jobs(id,kind,status,started_at,ended_at) "
        "VALUES(1,'backup','ok',10,20)"
    )
    connection.execute(
        "INSERT INTO pair_runs(job_id,pair_name,ok,status,started_at,ended_at,result_json) "
        "VALUES(1,'Fotos',1,'ok',10,20,?)",
        (json.dumps({"name": "Fotos", "ok": True, "dry_run": True}),),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    latest = database.pair_last_result("Fotos")
    assert latest is not None
    assert latest["pair"]["name"] == "Fotos"
    assert latest["history_key"] == "rclone:name:fotos"
    assert latest["dry_run"] is True
    assert database.pair_last_success("Fotos") is None
    with database.conn() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(pair_runs)").fetchall()
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert {"history_key", "dry_run", "scheduled_slot"} <= columns


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
    job_id = db.job_start(
        "backup",
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "scheduler",
            }
        ],
    )

    assert db.jobs_mark_all_running_stale(reason="test recovery") == 1
    job = db.job_get(job_id)
    assert job["status"] == "stale"
    pair = db.pair_last_result("Fotos")
    assert pair is not None
    assert pair["status"] == "stale"
    assert pair["pair"]["status"] == "stale"
    assert pair["pair"]["error"] == "test recovery"
    assert "pending" not in pair["pair"]


def test_job_specific_stale_transition_is_compare_and_set(tmp_path: Path):
    database = Database(tmp_path / "specific-stale.db")
    running = database.job_start(
        "backup",
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "scheduler",
            }
        ],
    )
    finished = database.job_start("backup")
    assert database.job_finish(finished, "ok", {"pairs": []}) is True

    assert database.job_mark_stale(running, "owner exited") is True
    assert database.job_mark_stale(running, "duplicate recovery") is False
    assert database.job_mark_stale(finished, "must not overwrite") is False
    assert database.job_get(running)["status"] == "stale"
    assert database.pair_last_result("Fotos")["status"] == "stale"
    assert database.job_get(finished)["status"] == "ok"


def test_age_recovery_can_be_limited_to_job_kind(tmp_path: Path):
    database = Database(tmp_path / "kind-stale.db")
    backup = database.job_start("backup")
    pbs = database.job_start("pbs")
    with database.conn() as connection:
        connection.execute(
            "UPDATE jobs SET started_at=? WHERE id IN (?, ?)",
            (time.time() - 3600, backup, pbs),
        )

    assert database.jobs_mark_stale(60, kind="pbs") == 1
    assert database.job_get(pbs)["status"] == "stale"
    assert database.job_get(backup)["status"] == "running"


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


def test_stable_history_queries_fall_back_to_legacy_pbs_pair_name(tmp_path: Path):
    database = Database(tmp_path / "pbs-history-fallback.db")
    legacy_job = database.job_start("pbs")
    database.job_finish(
        legacy_job,
        "ok",
        {"pairs": [{"name": "pbs:docs", "ok": True}]},
    )

    latest = database.pair_last_result("pbs:docs", history_key="pbs:id:stable-docs")
    success = database.pair_last_success("pbs:docs", history_key="pbs:id:stable-docs")

    assert latest is not None and latest["job_id"] == legacy_job
    assert success is not None and success["job_id"] == legacy_job


def test_stable_history_attempt_stops_legacy_success_fallback(tmp_path: Path):
    database = Database(tmp_path / "pbs-history-stable.db")
    legacy_job = database.job_start("pbs")
    database.job_finish(
        legacy_job,
        "ok",
        {"pairs": [{"name": "pbs:docs", "ok": True}]},
    )
    stable_key = "pbs:id:stable-docs"
    stable_job = database.job_start(
        "pbs",
        attempts=[{"name": "pbs:docs", "history_key": stable_key}],
    )
    database.job_finish(
        stable_job,
        "error",
        {"pairs": [{"name": "pbs:docs", "ok": False, "history_key": stable_key}]},
    )

    latest = database.pair_last_result("pbs:docs", history_key=stable_key)

    assert latest is not None and latest["job_id"] == stable_job
    assert database.pair_last_success("pbs:docs", history_key=stable_key) is None


def test_rclone_stable_history_attempt_stops_legacy_json_success_fallback(
    tmp_path: Path,
):
    database = Database(tmp_path / "rclone-history-stable.db")
    legacy_job = database.job_start("backup")
    database.job_finish(
        legacy_job,
        "ok",
        {"pairs": [{"name": "Fotos", "ok": True}]},
    )
    stable_key = "rclone:id:stable-photos"
    stable_job = database.job_start(
        "backup",
        attempts=[{"name": "Fotos", "history_key": stable_key}],
    )
    database.job_finish(
        stable_job,
        "error",
        {"pairs": [{"name": "Fotos", "ok": False, "history_key": stable_key}]},
    )

    assert database.pair_last_success("Fotos", history_key=stable_key) is None


def test_dry_run_is_never_counted_as_last_success(tmp_path: Path):
    database = Database(tmp_path / "dry-run.db")
    real = database.job_start("backup")
    database.job_finish(
        real,
        "ok",
        {"dry_run": False, "pairs": [{"name": "Fotos", "ok": True}]},
    )
    dry = database.job_start("backup")
    database.job_finish(
        dry,
        "ok",
        {"dry_run": True, "pairs": [{"name": "Fotos", "ok": True}]},
    )

    assert database.pair_last_result("Fotos")["job_id"] == dry
    assert database.pair_last_result("Fotos")["dry_run"] is True
    assert database.pair_last_success("Fotos")["job_id"] == real
    assert database.pair_last_successes()["Fotos"]["job_id"] == real


def test_terminal_job_finish_is_cas_and_cancelled_pairs_stay_cancelled(
    tmp_path: Path,
):
    database = Database(tmp_path / "cas.db")
    job_id = database.job_start("backup")
    assert (
        database.job_finish(
            job_id,
            "cancelled",
            {
                "cancelled": True,
                "pairs": [{"name": "Fotos", "ok": False, "skipped": True}],
            },
        )
        is True
    )
    assert database.job_finish(job_id, "ok", {"pairs": []}) is False
    assert database.job_get(job_id)["status"] == "cancelled"
    assert database.pair_last_result("Fotos")["status"] == "cancelled"


def test_prune_never_deletes_running_jobs(tmp_path: Path):
    database = Database(tmp_path / "prune.db")
    running = database.job_start("backup")
    terminal = database.job_start("backup")
    database.job_finish(terminal, "error", {"error": "old"})
    with database.conn() as connection:
        connection.execute(
            "UPDATE jobs SET started_at=? WHERE id IN (?, ?)",
            (time.time() - 3 * 86400, running, terminal),
        )

    assert database.jobs_prune(older_than_days=1, keep_latest=0) == 1
    assert database.job_get(running)["status"] == "running"
    assert database.job_get(terminal) is None


def test_job_and_pair_summaries_have_hard_byte_limits(tmp_path: Path):
    database = Database(tmp_path / "bounded.db")
    huge = "ä" * (db_module._MAX_JOB_SUMMARY_BYTES * 2)
    job_id = database.job_start("backup")
    database.job_finish(
        job_id,
        "error",
        {
            "ok": False,
            "error": huge,
            "pairs": [{"name": "Fotos", "ok": False, "command": huge}],
        },
    )

    with database.conn() as connection:
        row = connection.execute(
            "SELECT summary_json FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        pair_row = connection.execute(
            "SELECT result_json FROM pair_runs WHERE job_id=?", (job_id,)
        ).fetchone()
    assert len(row["summary_json"].encode("utf-8")) <= db_module._MAX_JOB_SUMMARY_BYTES
    assert (
        len(pair_row["result_json"].encode("utf-8")) <= db_module._MAX_PAIR_RESULT_BYTES
    )
    assert json.loads(row["summary_json"])["truncated"] is True
    assert json.loads(pair_row["result_json"])["truncated"] is True


def test_bulk_history_uses_typed_key_and_tracks_running_attempt(tmp_path: Path):
    database = Database(tmp_path / "typed-history.db")
    key = "rclone:id:photos"
    job_id = database.job_start(
        "backup",
        attempts=[
            {
                "name": "Fotos alt",
                "history_key": key,
                "trigger": "scheduler",
                "scheduled_slot": "slot-1",
            }
        ],
    )

    history = database.pair_last_history({key: "Fotos neu"})[key]
    assert history["last_result"]["job_id"] == job_id
    assert history["last_result"]["status"] == "running"
    assert history["last_result"]["pair"]["trigger"] == "scheduler"
    assert history["last_result"]["scheduled_slot"] == "slot-1"
    assert history["last_success"] is None


def test_exception_due_rows_keep_scheduler_trigger_for_retry(tmp_path: Path):
    database = Database(tmp_path / "exception.db")
    key = "rclone:id:photos"
    job_id = database.job_start("backup")
    database.job_finish(
        job_id,
        "error",
        {
            "ok": False,
            "error": "worker crashed",
            "due": ["Fotos"],
            "trigger": "scheduler",
            "history_keys": {"Fotos": key},
            "scheduler_slots": {"Fotos": "slot-1"},
        },
    )

    attempt = database.pair_last_history({key: "Fotos"})[key]["last_result"]
    assert attempt["status"] == "error"
    assert attempt["pair"]["trigger"] == "scheduler"
    assert attempt["pair"]["scheduled_slot"] == "slot-1"


def test_unfinished_attempt_result_is_terminalized_with_job(tmp_path: Path):
    database = Database(tmp_path / "unfinished-attempt.db")
    job_id = database.job_start(
        "backup",
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "scheduler",
            }
        ],
    )

    database.job_finish(job_id, "error", {"error": "worker crashed"})

    attempt = database.pair_last_result("Fotos", history_key="rclone:id:photos")
    assert attempt["status"] == "error"
    assert attempt["pair"]["status"] == "error"
    assert attempt["pair"]["ok"] is False
    assert attempt["pair"]["error"] == "worker crashed"
    assert "pending" not in attempt["pair"]


def test_exclusive_job_reservation_serializes_each_runtime_scope(tmp_path: Path):
    database = Database(tmp_path / "exclusive-scope.db")
    backup = database.job_start("backup", exclusive_scope=True)

    with pytest.raises(JobAlreadyRunningError):
        database.job_start("check", exclusive_scope=True)

    pbs = database.job_start("pbs", exclusive_scope=True)
    assert database.job_get(pbs)["status"] == "running"
    with pytest.raises(JobAlreadyRunningError):
        database.job_start("pbs", exclusive_scope=True)

    database.job_finish(backup, "ok", {})
    follow_up = database.job_start("quicksync", exclusive_scope=True)
    assert database.job_get(follow_up)["status"] == "running"


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


def test_runtime_settings_and_audit_are_persistent(tmp_path):
    database = Database(tmp_path / "events.db")
    database.runtime_set("feature", {"enabled": True})
    event_id = database.audit_add("config_saved", actor="admin", details={"pairs": 3})

    reopened = Database(tmp_path / "events.db")
    assert reopened.runtime_get("feature") == {"enabled": True}
    events = reopened.audit_list(limit=5)
    assert events[0]["id"] == event_id
    assert events[0]["actor"] == "admin"
    assert events[0]["details"] == {"pairs": 3}
    assert reopened.stats()["audit_events"] == 1
