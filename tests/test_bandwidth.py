import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import bandwidth
from app.auth import require_auth
from app.db import Database
from app.routes import api_bandwidth
from app.security import require_csrf

MIB = 1024**2
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc).timestamp()  # Montag


class _Cfg:
    def __init__(self, data):
        self._data = data

    def get(self, *path, default=None):
        node = self._data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _cfg(*jobs):
    return _Cfg({"backup": {"jobs": list(jobs), "timezone": "Europe/Berlin"}})


def _job(job_id, schedule, **extra):
    return {"id": job_id, "name": f"Job {job_id}", "schedule": schedule, **extra}


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _db(tmp_path, runs):
    """runs: (start_epoch, dauer_s, transferred, ok, dry_run)"""
    db = Database(tmp_path / "bandwidth.db")
    with db.conn() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,status,started_at) VALUES(1,'backup','ok',0)"
        )
        for index, (start, duration, transferred, ok, dry_run) in enumerate(runs):
            connection.execute(
                "INSERT INTO pair_runs(job_id,pair_name,ok,dry_run,status,"
                "started_at,ended_at,result_json) VALUES(1,?,?,?,?,?,?,?)",
                (
                    f"p{index}",
                    int(ok),
                    int(dry_run),
                    "ok" if ok else "error",
                    start,
                    start + duration,
                    json.dumps({"transferred": transferred}),
                ),
            )
    return db


def _run(start, duration=1024, transferred="1 GiB", ok=True, dry_run=False):
    return (start, duration, transferred, ok, dry_run)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0 B", 0),
        ("512 B", 512),
        ("1 KiB", 1024),
        ("1.5 MiB", 1572864),
        ("1,5 MiB", 1572864),
        ("2 GiB", 2 * 1024**3),
        ("1 TiB", 1024**4),
        ("5k", 5 * 1024),
        ("12M", 12 * MIB),
        ("3G", 3 * 1024**3),
        ("1T", 1024**4),
        ("2 Mi", 2 * MIB),
        (" 7 mib ", 7 * MIB),
        ("5", None),  # rclone-Zählzeile "5 / 10", keine Größe
        ("5 files", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_size(text, expected):
    assert bandwidth.parse_size(text) == expected


def test_parse_rate():
    assert bandwidth.parse_rate("12.5 MiB/s") == 12.5 * MIB
    assert bandwidth.parse_rate("1,5 KiB/s") == 1536.0
    assert bandwidth.parse_rate("0 B/s") == 0.0
    assert bandwidth.parse_rate("-") is None
    assert bandwidth.parse_rate(None) is None


def test_heatmap_buckets_by_start_slot_across_dst(tmp_path):
    db = _db(
        tmp_path,
        [
            _run(_utc(2026, 7, 15, 0, 30)),  # Mi, MESZ  -> 02:30
            _run(_utc(2026, 1, 14, 1, 30), transferred="3 GiB"),  # Mi, MEZ -> 02:30
            _run(_utc(2026, 7, 14, 23, 30)),  # Di 23:30 UTC -> Mi 01:30 Berlin
        ],
    )

    hm = bandwidth.heatmap(db, days=365, now=NOW)

    assert hm["timezone"] == "Europe/Berlin" and hm["days"] == 365
    assert len(hm["cells"]) == 7 and all(len(row) == 24 for row in hm["cells"])
    assert hm["samples"][2][2] == 2
    assert hm["cells"][2][2] == pytest.approx(2 * MIB)  # Mittel aus 1 und 3 MiB/s
    assert hm["samples"][2][1] == 1 and hm["cells"][2][1] == pytest.approx(MIB)
    assert sum(map(sum, hm["samples"])) == 3
    assert hm["cells"][0][0] is None

    recent = bandwidth.heatmap(db, days=90, now=NOW)  # Januar-Lauf fällt heraus
    assert recent["samples"][2][2] == 1


def test_heatmap_filters_unusable_runs(tmp_path):
    monday_10 = _utc(2026, 7, 13, 8, 0)  # 10:00 MESZ
    db = _db(
        tmp_path,
        [
            _run(monday_10, dry_run=True),
            _run(monday_10, ok=False),
            _run(monday_10, duration=29),
            _run(monday_10, transferred="0 B"),
            _run(monday_10, transferred="5"),  # Dateizähler statt Bytes
            _run(monday_10, transferred=None),
            _run(monday_10, duration=30, transferred="30 MiB"),  # gilt (Grenze)
        ],
    )

    hm = bandwidth.heatmap(db, now=NOW)

    assert hm["samples"][0][10] == 1
    assert hm["cells"][0][10] == pytest.approx(MIB)
    assert sum(map(sum, hm["samples"])) == 1


def test_scheduled_slots_expands_enabled_cron_jobs():
    cfg = _cfg(
        _job("a", "0 3 * * *"),
        _job("b", "0 5 * * *", enabled=False),
        _job("c", "manual"),
        _job("d", "30 2 * * 3"),
        _job("e", "kaputt"),
    )

    slots = bandwidth.scheduled_slots(cfg, timezone_name="Europe/Berlin", now=NOW)

    assert slots == {(day, 3) for day in range(7)} | {(2, 2)}


def test_suggest_windows_avoids_busy_slots_and_labels_in_german():
    cells = [[1.0] * 24 for _ in range(7)]
    cells[2][2] = 1.5
    hm = {"cells": cells}

    best = bandwidth.suggest_windows(hm, set())[0]
    assert (best["weekday"], best["hour"]) == (2, 2)
    assert best["label"] == "Mittwoch 02:00–03:00"

    busy = {(2, 2)}
    windows = bandwidth.suggest_windows(hm, busy, limit=3)
    assert len(windows) == 3
    assert all((w["weekday"], w["hour"]) not in busy for w in windows)
    assert windows[0]["score"] >= windows[-1]["score"]
    assert bandwidth.slot_label(0, 23) == "Montag 23:00–00:00"


def test_suggest_windows_without_measurements_prefers_night_and_fills_median():
    empty = {"cells": [[None] * 24 for _ in range(7)]}
    windows = bandwidth.suggest_windows(empty, set())
    assert len(windows) == 5 and all(w["hour"] >= 22 or w["hour"] < 6 for w in windows)

    one = [[None] * 24 for _ in range(7)]
    one[2][14] = 4.0
    assert bandwidth.suggest_windows({"cells": one}, set(), limit=1)[0]["score"] == 1.25


_HISTORY = [
    _run(_utc(2026, 7, 15, 2, 0), duration=100, transferred="10 GiB"),  # Mi 04:00
    _run(_utc(2026, 7, 15, 14, 0), duration=100, transferred="1 GiB"),  # Mi 16:00
]


def test_propose_keeps_minute_and_weekday(tmp_path):
    db = _db(tmp_path, _HISTORY)
    cfg = _cfg(_job("w", "15 14 * * 3"))

    proposals = bandwidth.propose_schedules(
        cfg, db, timezone_name="Europe/Berlin", now=NOW
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["definition_id"] == "w" and proposal["name"] == "Job w"
    assert proposal["current_schedule"] == "15 14 * * 3"
    assert proposal["proposed_schedule"] == "15 4 * * 3"
    assert (proposal["weekday"], proposal["hour"]) == (2, 4)
    assert "Nachtfenster" in proposal["reason"]


def test_propose_keeps_daily_frequency_and_skips_unsafe_schedules(tmp_path):
    db = _db(tmp_path, _HISTORY)
    cfg = _cfg(
        _job("daily", "0 14 * * *"),
        _job("often", "0 */6 * * *"),  # eine Stunde würde die Häufigkeit senken
        _job("monthly", "0 14 1 * *"),
        _job("off", "0 14 * * *", enabled=False),
        _job("manual", "manual"),
    )

    proposals = bandwidth.propose_schedules(
        cfg, db, timezone_name="Europe/Berlin", now=NOW
    )

    assert [p["definition_id"] for p in proposals] == ["daily"]
    assert proposals[0]["proposed_schedule"] == "0 4 * * *"


def test_propose_skips_job_already_in_best_window(tmp_path):
    db = _db(tmp_path, _HISTORY)

    assert (
        bandwidth.propose_schedules(
            _cfg(_job("w", "0 4 * * 3")), db, timezone_name="Europe/Berlin", now=NOW
        )
        == []
    )


def test_routes_validate_days_and_return_suggestions(tmp_path, monkeypatch):
    db = _db(tmp_path, _HISTORY)
    monkeypatch.setattr(api_bandwidth, "get_db", lambda: db)
    monkeypatch.setattr(
        api_bandwidth, "get_config", lambda: _cfg(_job("w", "15 14 * * 3"))
    )
    app = FastAPI()
    app.include_router(api_bandwidth.router)
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app)

    assert client.get("/api/jobs/bandwidth/heatmap?days=3").status_code == 422
    assert client.get("/api/jobs/bandwidth/heatmap?days=366").status_code == 422
    heat = client.get("/api/jobs/bandwidth/heatmap?days=365").json()
    assert heat["days"] == 365 and len(heat["cells"]) == 7
    assert client.get("/api/jobs/bandwidth/heatmap").json()["days"] == 90

    suggestions = client.get("/api/jobs/bandwidth/suggestions").json()
    assert suggestions["timezone"] == "Europe/Berlin"
    assert len(suggestions["windows"]) == 5
    assert [s["definition_id"] for s in suggestions["schedules"]] == ["w"]
