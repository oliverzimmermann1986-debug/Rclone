import asyncio
import csv
import io

import pytest

from app.db import Database
from app.routes import api_jobs


def partial_summary():
    return {
        "ok": False,
        "pairs": [
            {
                "name": "Fotos",
                "ok": False,
                "sample_status": "partial_selection",
                "verified": 19,
                "sample_size": 19,
                "requested_sample_size": 20,
                "restored_files": 19,
                "return_code": 0,
                "sample_shortfall_reason": "byte_budget",
            }
        ],
    }


def test_historical_warning_filters_counts_exports_and_badges_agree(tmp_path):
    db = Database(tmp_path / "filters.db")
    historic = db.job_start("restoretest")
    db.job_finish(historic, "error", partial_summary())
    current = db.job_start("restoretest")
    db.job_finish(current, "warning", partial_summary())
    failed = db.job_start("restoretest")
    mixed = partial_summary()
    mixed["pairs"].append({"name": "Rezepte", "ok": False, "error": "mismatch"})
    db.job_finish(failed, "error", mixed)

    assert db.job_count(status="warning") == 2
    assert db.job_count(status="error") == 1
    assert {job["id"] for job in db.job_list(status="warning")} == {historic, current}
    assert [job["id"] for job in db.job_list(status="error")] == [failed]
    rows, count = db.job_search(kind="restoretest", status="warning", limit=1, offset=1)
    assert count == 2 and len(rows) == 1
    assert rows[0]["id"] == historic
    assert {job["id"] for job in db.job_iter(status="warning", batch_size=1)} == {
        historic,
        current,
    }
    assert db.job_statistics()["by_status"] == {"warning": 2, "error": 1}
    with db.conn() as connection:
        assert (
            connection.execute(
                "SELECT status FROM jobs WHERE id=?", (historic,)
            ).fetchone()[0]
            == "error"
        )
    assert db.job_get(historic)["display_status"] == "warning"


@pytest.mark.parametrize(
    "summary_json",
    ["not json", "[]", "null", "true", '{"truncated": true, "pairs": []}'],
)
def test_unproven_history_remains_in_error_filter(tmp_path, summary_json):
    db = Database(tmp_path / "malformed.db")
    job_id = db.job_start("restoretest")
    db.job_finish(job_id, "error", {"ok": False})
    with db.conn() as connection:
        connection.execute(
            "UPDATE jobs SET summary_json=? WHERE id=?", (summary_json, job_id)
        )
    assert db.job_count(status="warning") == 0
    assert db.job_count(status="error") == 1
    assert db.job_statistics()["by_status"] == {"error": 1}


def test_non_restore_failures_never_use_partial_projection(tmp_path):
    db = Database(tmp_path / "backup.db")
    db.job_finish(db.job_start("backup"), "error", partial_summary())
    assert db.job_count(status="warning") == 0
    assert db.job_count(status="error", kind="backup") == 1
    assert db.job_statistics()["by_status"] == {"error": 1}


def test_failed_cleanup_preserves_historical_partial_warnings(tmp_path):
    db = Database(tmp_path / "cleanup.db")
    warning = db.job_start("restoretest")
    db.job_finish(warning, "error", partial_summary())
    failed = db.job_start("restoretest")
    db.job_finish(failed, "error", {"ok": False, "error": "mismatch"})
    assert db.jobs_delete_failed() == 1
    assert db.job_get(failed) is None
    assert db.job_get(warning)["status"] == "error"
    assert db.job_get(warning)["display_status"] == "warning"


def test_csv_explains_display_warning_without_rewriting_execution_status(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "export.db")
    warning = db.job_start("restoretest")
    db.job_finish(warning, "error", partial_summary())
    monkeypatch.setattr(api_jobs, "get_db", lambda: db)
    response = api_jobs.export_jobs_csv(
        kind="restoretest", status="warning", q="", limit=100
    )

    async def read_body():
        return "".join([chunk async for chunk in response.body_iterator])

    rows = list(csv.DictReader(io.StringIO(asyncio.run(read_body()).lstrip("\ufeff"))))
    assert len(rows) == 1
    assert rows[0]["id"] == str(warning)
    assert rows[0]["status"] == "error"
    assert rows[0]["anzeigestatus"] == "warning"
