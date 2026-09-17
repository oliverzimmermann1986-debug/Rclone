"""Verified subsets are warnings, never successful complete restore proofs."""

import copy
from types import SimpleNamespace

import pytest

from app.db import Database
from app.jobs import scheduler_cli
from app.protection import protection_calendar
from app.restore_evidence import is_partial_restore_summary
from app.routes import api_diagnostics, api_jobs


def _partial():
    return {
        "name": "Fotos",
        "history_key": "restore:id:photos",
        "ok": False,
        "sample_status": "partial_selection",
        "requested_sample_size": 20,
        "sample_size": 19,
        "verified": 19,
        "restored_files": 19,
        "return_code": 0,
        "sample_shortfall": 1,
        "sample_shortfall_reason": "byte_budget",
        "budget_bytes": 256 * 1024 * 1024,
        "sample_manifest_sha256": "a" * 64,
        "error": "Teil-Stichprobe: 19 von 20 angeforderten Dateien ausgewählt und erfolgreich geprüft",
    }


def _summary():
    return {
        "kind": "restoretest",
        "ok": False,
        "pairs": [
            _partial(),
            {
                "name": "restore-drill",
                "ok": False,
                "sample_size": 19,
                "verified": 19,
                "pairs_tested": 1,
            },
        ],
    }


def _large_mixed_summary():
    # Exercise the real bounded JSON serializer, not a handwritten truncated
    # payload. Its preview retains the partial result but omits the late error.
    pairs = [
        _partial(),
        *[
            {"name": f"pair-{index}", "ok": True, "diagnostics": "x" * 3000}
            for index in range(100)
        ],
        {"name": "failed-at-end", "ok": False, "error": "checksum mismatch"},
    ]
    return {
        "kind": "restoretest",
        "ok": False,
        "pairs": [
            *pairs,
            {"name": "restore-drill", "ok": False, "pairs_tested": len(pairs)},
        ],
    }


def test_actual_bounded_mixed_summary_preserves_error_projections(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "truncated-history.db")
    summary = _large_mixed_summary()
    assert is_partial_restore_summary(summary) is False
    job_id = db.job_start("restoretest")
    assert db.job_finish(job_id, "error", summary)
    job = db.job_get(job_id)
    stored = job["summary"]
    assert stored["truncated"] is True
    assert len(stored["pairs"]) < len(summary["pairs"])
    assert all(pair["name"] != "failed-at-end" for pair in stored["pairs"])
    assert is_partial_restore_summary(stored) is False
    assert job["status"] == "error"
    assert "display_status" not in job
    assert api_diagnostics._verified_partial_job(job) is False
    assert api_diagnostics._last_job_alert(job)["level"] == "error"
    day = protection_calendar(db, days=7, timezone_name="Europe/Berlin")[0]
    assert day["failed"] == 1 and day["warnings"] == 0
    assert day["state"] == "error"
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    assert api_jobs._finish_status(stored) == "error"
    assert scheduler_cli._job_status(stored) == "error"


@pytest.mark.parametrize("terminal_staging", [False, True])
def test_warning_persists_but_does_not_become_success(tmp_path, terminal_staging):
    db = Database(tmp_path / "warnings.db")
    job_id = db.job_start("restoretest")
    finish = db.job_finish_external if terminal_staging else db.job_finish
    assert finish(job_id, "warning", _summary())
    job = db.job_get(job_id)
    assert job["status"] == "warning"
    assert job["summary"]["ok"] is False
    assert job["display_status"] == "warning"
    with db.conn() as connection:
        rows = connection.execute(
            "SELECT pair_name,ok,status FROM pair_runs WHERE job_id=?", (job_id,)
        ).fetchall()
    assert {row["pair_name"] for row in rows} == {"Fotos", "restore-drill"}
    assert all(row["ok"] == 0 and row["status"] == "warning" for row in rows)
    history = db.pair_last_history({"restore:id:photos": "Fotos"})["restore:id:photos"]
    assert history["last_success"] is None
    assert history["last_result"]["status"] == "warning"
    assert db.job_statistics()["by_status"] == {"warning": 1}


def test_historical_error_is_not_rewritten_but_display_is_warning(tmp_path):
    db = Database(tmp_path / "history.db")
    job_id = db.job_start("restoretest")
    historic = _summary()
    historic.pop("kind")  # shape of the actual pre-fix persisted result
    assert db.job_finish(job_id, "error", historic)
    job = db.job_get(job_id)
    assert job["status"] == "error"
    assert job["display_status"] == "warning"
    with db.conn() as connection:
        assert (
            connection.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()[0]
            == "error"
        )
    calendar = protection_calendar(db, days=7, timezone_name="Europe/Berlin")
    assert calendar[0]["failed"] == 0
    assert calendar[0]["successful"] == 0
    assert calendar[0]["warnings"] == 1
    assert calendar[0]["state"] == "warning"


def test_actual_error_still_dominates_calendar_warning(tmp_path):
    db = Database(tmp_path / "calendar.db")
    db.job_finish(db.job_start("restoretest"), "warning", _summary())
    db.job_finish(db.job_start("backup"), "error", {"ok": False, "error": "timeout"})
    day = protection_calendar(db, days=7, timezone_name="Europe/Berlin")[0]
    assert day["state"] == "error"
    assert day["warnings"] == 1 and day["failed"] == 1


def test_manual_and_scheduler_finish_agree_without_masking_errors(monkeypatch):
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    for finish in (api_jobs._finish_status, scheduler_cli._job_status):
        assert finish(_summary()) == "warning"
        assert finish({**_summary(), "kind": "backup"}) == "error"
        assert finish({**_summary(), "cancelled": True}) == "cancelled"
        mixed = _summary()
        mixed["pairs"].append(
            {"name": "Rezepte", "ok": False, "error": "checksum mismatch"}
        )
        assert finish(mixed) == "error"
        cleanup = _summary()
        cleanup["pairs"][0]["temp_cleanup_failed"] = True
        assert finish(cleanup) == "error"


def test_warning_filter_is_accepted_by_list_and_search(tmp_path, monkeypatch):
    db = Database(tmp_path / "filter.db")
    job_id = db.job_start("restoretest")
    db.job_finish(job_id, "warning", _summary())
    monkeypatch.setattr(api_jobs, "get_db", lambda: db)
    assert (
        api_jobs.list_jobs(kind="restoretest", status="warning", q="")[0]["id"]
        == job_id
    )
    found = api_jobs.search_jobs(kind="restoretest", status="warning", q="")
    assert found["total"] == 1 and found["items"][0]["status"] == "warning"


def test_alert_is_specific_links_job_and_recognizes_legacy_partial():
    partial = {
        "id": 81,
        "kind": "restoretest",
        "status": "error",
        "summary": _summary(),
    }
    warning = api_diagnostics._last_job_alert(partial)
    assert warning["level"] == "warn"
    assert warning["job_id"] == 81 and warning["source"] == "last_job"
    assert "Prüfumfang begrenzt" in warning["message"]
    assert "fehlgeschlagen" not in warning["message"]
    failed = copy.deepcopy(partial)
    failed["summary"]["pairs"][0]["return_code"] = 1
    error = api_diagnostics._last_job_alert(failed)
    assert error["level"] == "error"
    assert "Restore-Prüfung" in error["message"] and "#81" in error["message"]
    assert api_diagnostics._last_job_alert({"kind": "backup", "status": "ok"}) is None


@pytest.mark.parametrize("message", [None, "", "  \n", "old error"])
def test_successful_pair_never_exposes_error(message):
    assert (
        api_diagnostics._pair_failure_text({"status": "ok", "pair": {"error": message}})
        is None
    )


def test_failed_pair_has_meaningful_fallback():
    assert api_diagnostics._pair_failure_text(
        {"status": "error", "pair": {"error": "  "}}
    ).strip()
    assert (
        api_diagnostics._pair_failure_text(
            {"status": "error", "pair": {"error": " timeout "}}
        )
        == "timeout"
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"error": "Pre-Check fehlgeschlagen (remote: Timeout nach 15s / local: ok)"},
        {
            "error": "Unerwarteter Rückgang der Quelle: Destruktiver Lauf wurde quarantänisiert.",
            "quarantined": True,
        },
    ],
    ids=["remote-precheck-timeout", "anomaly-quarantine"],
)
def test_skipped_failure_remains_visible_in_data_path_health(
    tmp_path, monkeypatch, failure
):
    db = Database(tmp_path / "skipped-failure.db")
    pair = {
        "id": "recipes",
        "name": "Rezepte",
        "remote": "cloud:/recipes",
        "local": str(tmp_path / "recipes"),
        "direction": "pull",
        "mode": "copy",
    }
    job_id = db.job_start("backup")
    db.job_finish(
        job_id,
        "error",
        {
            "ok": False,
            "pairs": [
                {
                    "name": pair["name"],
                    "history_key": api_diagnostics.rclone_history_key(pair),
                    "ok": False,
                    "skipped": True,
                    **failure,
                }
            ],
        },
    )
    cfg = {
        "backup": {"enabled": True, "pairs": [pair], "jobs": []},
        "paths": {"data_dir": str(tmp_path)},
    }
    monkeypatch.setattr(
        api_diagnostics, "get_config", lambda: SimpleNamespace(snapshot=lambda: cfg)
    )
    monkeypatch.setattr(api_diagnostics, "get_db", lambda: db)
    monkeypatch.setattr(
        api_diagnostics,
        "operational_snapshot",
        lambda **_: {
            "services": {
                "scheduler": ("enabled", "active"),
                "web": ("enabled", "active"),
            },
            "system": {"memory": {"percent_used": 1}, "disk": {}},
        },
    )
    monkeypatch.setattr(
        api_diagnostics, "scheduler_state", lambda *_args, **_kwargs: {}
    )

    overview = api_diagnostics._build_overview()
    health = overview["pairs"]["health"][0]
    assert health["last_status"] == "skipped"
    assert health["job_id"] == job_id
    assert health["error"] == failure["error"]
    assert overview["jobs"]["last_error"]["id"] == job_id


@pytest.mark.parametrize(
    "pair",
    [
        {"ok": False, "reason": "Kein Lauf vorgesehen"},
        {"ok": False, "error": "  \n"},
        {"ok": True, "error": "Alter Fehler"},
        {"ok": False, "cancelled": True, "error": "Vor Start abgebrochen"},
    ],
)
def test_skipped_without_current_failure_never_creates_health_error(pair):
    assert (
        api_diagnostics._pair_failure_text({"status": "skipped", "pair": pair}) is None
    )


@pytest.mark.parametrize("truncated_mixed", [False, True])
def test_overview_keeps_successful_backup_separate_from_partial_check(
    tmp_path, monkeypatch, truncated_mixed
):
    db = Database(tmp_path / "overview.db")
    backup_id = db.job_start("backup")
    db.job_finish(backup_id, "ok", {"ok": True})
    restore_id = db.job_start("restoretest")
    db.job_finish(
        restore_id, "error", _large_mixed_summary() if truncated_mixed else _summary()
    )
    cfg = {
        "backup": {"enabled": True, "pairs": [], "jobs": []},
        "paths": {"data_dir": str(tmp_path)},
    }
    monkeypatch.setattr(
        api_diagnostics, "get_config", lambda: SimpleNamespace(snapshot=lambda: cfg)
    )
    monkeypatch.setattr(api_diagnostics, "get_db", lambda: db)
    monkeypatch.setattr(
        api_diagnostics,
        "operational_snapshot",
        lambda **_: {
            "services": {
                "scheduler": ("enabled", "active"),
                "web": ("enabled", "active"),
            },
            "system": {"memory": {"percent_used": 1}, "disk": {}},
        },
    )
    monkeypatch.setattr(
        api_diagnostics, "scheduler_state", lambda *_args, **_kwargs: {}
    )
    overview = api_diagnostics._build_overview()
    assert overview["jobs"]["last"]["id"] == restore_id
    assert overview["jobs"]["last_success"]["id"] == backup_id
    if truncated_mixed:
        assert overview["jobs"]["last_error"]["id"] == restore_id
    else:
        assert overview["jobs"]["last_error"] is None
    alerts = [a for a in overview["alerts"] if a.get("source") == "last_job"]
    assert len(alerts) == 1
    assert alerts[0]["level"] == ("error" if truncated_mixed else "warn")


def test_real_failed_backup_does_not_gain_partial_display_status(tmp_path):
    db = Database(tmp_path / "failure.db")
    job_id = db.job_start("backup")
    db.job_finish(job_id, "error", _summary())
    assert "display_status" not in db.job_get(job_id)
