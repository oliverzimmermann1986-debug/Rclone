import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app import protection
from app.jobs.scheduler import next_run_after
from app.protection import (
    POLICY_PRESETS,
    acknowledge_quarantine,
    anomaly_status,
    evaluate_anomaly,
    preflight_anomaly_guard,
    protection_calendar,
    record_anomaly_baseline,
    score_components,
)
from app.restore_evidence import MAX_EVIDENCE_AGE_SEC


def _pair() -> dict[str, object]:
    return {
        "id": "a" * 32,
        "name": "Fotos",
        "local": "/mnt/fotos",
        "remote": "pcloud:/Fotos",
        "direction": "push",
        "mode": "sync",
        "allow_delete": True,
        "max_delete": 25,
    }


def test_anomaly_evaluation_fails_closed_only_with_reliable_baseline():
    settings = {
        "min_baseline_files": 100,
        "file_drop_percent": 35,
        "size_drop_percent": 35,
    }
    finding = evaluate_anomaly(
        {"count": 1000, "bytes": 10_000},
        {"ok": True, "count": 500, "bytes": 5_000},
        settings,
    )
    assert finding["blocked"] is True
    assert finding["file_drop_percent"] == 50

    assert (
        evaluate_anomaly(
            {"count": 20, "bytes": 10_000},
            {"ok": True, "count": 1, "bytes": 10},
            settings,
        )["blocked"]
        is False
    )
    assert (
        evaluate_anomaly(
            {"count": 1000, "bytes": 10_000},
            {"ok": False, "error": "timeout"},
            settings,
        )["blocked"]
        is False
    )


def test_anomaly_quarantine_persists_until_acknowledged(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "guard.db")
    pair = _pair()
    record_anomaly_baseline(
        database,
        pair=pair,
        measurement={"ok": True, "count": 1000, "bytes": 50_000},
    )
    monkeypatch.setattr(
        "app.protection.measure_path",
        lambda *_args, **_kwargs: {
            "ok": True,
            "count": 100,
            "bytes": 5_000,
            "measured_at": time.time(),
        },
    )

    finding = preflight_anomaly_guard(
        database,
        pair=pair,
        backup={"anomaly_guard": {"enabled": True}},
        source="/mnt/fotos",
    )
    assert finding["blocked"] is True
    status = anomaly_status(database, [pair])
    assert status["active"] == 1
    assert "source" not in status["items"][0]
    assert "measurement" not in status["items"][0]
    assert acknowledge_quarantine(database, pair) is True
    assert anomaly_status(database, [pair])["active"] == 0
    assert acknowledge_quarantine(database, pair) is False


def test_non_destructive_copy_skips_measurement(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "copy.db")
    pair = {**_pair(), "mode": "copy", "allow_delete": False}
    monkeypatch.setattr(
        "app.protection.measure_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert (
        preflight_anomaly_guard(database, pair=pair, backup={}, source="/mnt/fotos")[
            "blocked"
        ]
        is False
    )


def test_runtime_update_serializes_independent_connections(tmp_path: Path):
    database = Database(tmp_path / "atomic.db")
    connections = [Database(database.path) for _ in range(8)]
    barrier = threading.Barrier(len(connections))

    def increment(connection):
        barrier.wait(timeout=5)
        for _ in range(8):
            connection.runtime_update("counter", lambda value: value + 1, default=0)

    with ThreadPoolExecutor(max_workers=len(connections)) as pool:
        list(pool.map(increment, connections))
    assert database.runtime_get("counter") == 64


def test_guard_keeps_parallel_baseline_and_acknowledgement(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "concurrent-guard.db")
    other_connection = Database(database.path)
    pair = _pair()
    other_pair = {**pair, "id": "b" * 32, "name": "Dokumente", "local": "/mnt/docs"}
    record_anomaly_baseline(
        database, pair=pair, measurement={"ok": True, "count": 1000, "bytes": 50_000}
    )
    old_quarantine_key = protection._state_key(other_pair)
    state = database.runtime_get(protection._ANOMALY_STATE_KEY)
    state["quarantines"][old_quarantine_key] = {"detected_at": time.time()}
    database.runtime_set(protection._ANOMALY_STATE_KEY, state)
    started = threading.Event()
    resume = threading.Event()

    def measure(*_args, **_kwargs):
        started.set()
        assert resume.wait(timeout=5)
        return {"ok": True, "count": 10, "bytes": 50}

    monkeypatch.setattr(protection, "measure_path", measure)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            preflight_anomaly_guard,
            database,
            pair=pair,
            backup={},
            source="/mnt/photos",
        )
        try:
            assert started.wait(timeout=5)
            record_anomaly_baseline(
                other_connection,
                pair=other_pair,
                measurement={"ok": True, "count": 500, "bytes": 5000},
            )
            assert acknowledge_quarantine(other_connection, other_pair)
        finally:
            resume.set()
        assert future.result(timeout=5)["blocked"]
    state = database.runtime_get(protection._ANOMALY_STATE_KEY)
    assert old_quarantine_key in state["baselines"]
    assert old_quarantine_key not in state["quarantines"]
    assert protection._state_key(pair) in state["quarantines"]


def test_protection_calendar_and_score_are_evidence_based(tmp_path: Path):
    database = Database(tmp_path / "calendar.db")
    ok_job = database.job_start("backup")
    database.job_finish(ok_job, "ok", {"pairs": []})
    failed_job = database.job_start("restoretest")
    database.job_finish(failed_job, "error", {"pairs": []})
    days = protection_calendar(database, days=30, timezone_name="Europe/Berlin")
    assert sum(day["total"] for day in days) == 2
    assert sum(day["restore_tests"] for day in days) == 1
    assert any(day["state"] == "error" for day in days)

    score = score_components(
        overview={
            "pairs": {
                "total": 1,
                "enabled": 1,
                "scheduled": 1,
                "health": [{"last_status": "ok", "overdue": False}],
            }
        },
        storage={"pairs": [{"restore_evidence": {"state": "passed", "valid": True}}]},
        config={"backup": {"pairs": [{**_pair(), "backup_dir": "versions"}]}},
    )
    assert score["score"] == 100
    assert score["state"] == "ready"


def test_policy_presets_have_unique_stable_identifiers():
    identifiers = [item["id"] for item in POLICY_PRESETS]
    assert len(identifiers) == len(set(identifiers))
    assert {"family_photos", "documents", "archive", "critical"} == set(identifiers)


def test_restore_preset_recurrence_matches_evidence_window():
    # UTC checks the calendar policy independently of local DST/runtime delays.
    # A monthly recommendation used to leave three weeks without valid proof.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for preset in POLICY_PRESETS:
        schedule = preset["restore"]["schedule"]
        previous = next_run_after(schedule, after=start, timezone_name="UTC")
        for _ in range(60):
            following = next_run_after(schedule, after=previous, timezone_name="UTC")
            assert 0 < following - previous <= MAX_EVIDENCE_AGE_SEC, preset["id"]
            previous = following
