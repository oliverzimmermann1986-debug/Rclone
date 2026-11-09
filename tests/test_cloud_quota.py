from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import cloud_quota
from app.auth import require_auth
from app.db import Database
from app.routes import api_quota
from app.security import require_csrf

GIB = 1024**3
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp()


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_budget_settings_and_configured_remotes():
    cfg = {
        "backup": {
            "transfer_budget": {
                "enabled": 1,
                "monthly_egress_gb": "50",
                "monthly_upload_gb": -5,
                "warn_percent": 5,
                "throttle_optional_jobs": False,
            },
            "pairs": [
                {
                    "name": "Fotos",
                    "local": "/srv/photos",
                    "remote": "b2:bucket/photos",
                },
                {
                    "name": "WindowsPull",
                    "local": r"C:\Backups\Docs",
                    "remote": "gdrive:docs",
                },
                {
                    "name": "Dup",
                    "local": "b2:other",
                    "remote": "s3:archive/data",
                },
            ],
        }
    }
    settings = cloud_quota.budget_settings(cfg)
    assert settings == {
        "enabled": True,
        "monthly_egress_gb": 50,
        "monthly_upload_gb": 0,
        "warn_percent": 10,
        "throttle_optional_jobs": False,
    }
    assert cloud_quota.configured_remotes(cfg) == ["b2", "gdrive", "s3"]


def test_remote_about_success_and_error_cases():
    payload = {
        "total": 100 * GIB,
        "used": 25 * GIB,
        "free": 70 * GIB,
        "trashed": 5 * GIB,
    }
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _FakeCompleted(stdout=json.dumps(payload), returncode=0)

    res = cloud_quota.remote_about("b2:", run=fake_run)
    assert calls == [["rclone", "about", "--json", "--", "b2:"]]
    assert res["ok"] is True
    assert res["remote"] == "b2"
    assert res["total_bytes"] == 100 * GIB
    assert res["used_bytes"] == 25 * GIB
    assert res["free_bytes"] == 70 * GIB
    assert res["trashed_bytes"] == 5 * GIB
    assert res["other_bytes"] is None
    assert res["percent_used"] == 25.0
    assert res["error"] is None

    failed = cloud_quota.remote_about(
        "b2",
        run=lambda *_a, **_k: _FakeCompleted(
            stderr="about not supported", returncode=1
        ),
    )
    assert failed["ok"] is False
    assert "about not supported" in (failed["error"] or "")

    def timeout_run(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    timed_out = cloud_quota.remote_about("b2", run=timeout_run)
    assert timed_out["ok"] is False
    assert "Zeitüberschreitung" in (timed_out["error"] or "")

    invalid = cloud_quota.remote_about("-bad")
    assert invalid["ok"] is False


def test_estimate_growth_and_days_left(tmp_path: Path):
    db = Database(tmp_path / "growth.db")
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO jobs(id, kind, status, started_at) VALUES (1, 'backup', 'ok', ?)",
            (NOW - 86400,),
        )
        runs = [
            # Counts: push to b2 within 30d window (15 GiB)
            (
                "p1",
                1,
                0,
                NOW - 5 * 86400,
                {"remote": "b2:bucket/a", "direction": "push", "transferred": "15 GiB"},
            ),
            # Counts: bisync to b2 within 30d window (15 GiB)
            (
                "p2",
                1,
                0,
                NOW - 2 * 86400,
                {
                    "remote": "b2:bucket/b",
                    "direction": "bisync",
                    "transferred": "15 GiB",
                },
            ),
            # Ignored: pull direction
            (
                "p3",
                1,
                0,
                NOW - 2 * 86400,
                {"remote": "b2:bucket/c", "direction": "pull", "transferred": "90 GiB"},
            ),
            # Ignored: dry_run=1
            (
                "p4",
                1,
                1,
                NOW - 1 * 86400,
                {"remote": "b2:bucket/d", "direction": "push", "transferred": "60 GiB"},
            ),
            # Ignored: outside 30d window
            (
                "p5",
                1,
                0,
                NOW - 40 * 86400,
                {"remote": "b2:bucket/e", "direction": "push", "transferred": "60 GiB"},
            ),
            # Ignored: other remote
            (
                "p6",
                1,
                0,
                NOW - 1 * 86400,
                {"remote": "s3:other", "direction": "push", "transferred": "60 GiB"},
            ),
        ]
        for idx, (name, ok, dry, start, result) in enumerate(runs, start=1):
            conn.execute(
                "INSERT INTO pair_runs(job_id, pair_name, ok, dry_run, status, started_at, ended_at, result_json) "
                "VALUES (1, ?, ?, ?, 'ok', ?, ?, ?)",
                (f"{name}-{idx}", ok, dry, start, start + 60, json.dumps(result)),
            )

    # 30 GiB over 30 days = 1 GiB/day; 10 GiB free -> 10.0 days left
    growth = cloud_quota.estimate_growth_and_days_left(
        db, "b2", 10 * GIB, now=NOW, window_days=30
    )
    assert growth["daily_upload_bytes"] == GIB
    assert growth["days_until_full"] == 10.0


def test_monthly_transfer_usage_and_throttling(tmp_path: Path):
    db = Database(tmp_path / "budget.db")
    oct_5 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    sep_25 = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc).timestamp()

    with db.conn() as conn:
        conn.execute(
            "INSERT INTO jobs(id, kind, status, started_at, dry_run) VALUES (1, 'backup', 'ok', ?, 0)",
            (oct_5,),
        )
        conn.execute(
            "INSERT INTO jobs(id, kind, status, started_at, dry_run) VALUES (2, 'backup', 'ok', ?, 0)",
            (sep_25,),
        )
        # Current month push: 8 GiB upload
        conn.execute(
            "INSERT INTO pair_runs(job_id, pair_name, ok, dry_run, status, started_at, ended_at, result_json) "
            "VALUES (1, 'Fotos', 1, 0, 'ok', ?, ?, ?)",
            (
                oct_5,
                oct_5 + 60,
                json.dumps(
                    {"remote": "b2:photos", "direction": "push", "transferred": "8 GiB"}
                ),
            ),
        )
        # Current month pull: 6 GiB egress
        conn.execute(
            "INSERT INTO pair_runs(job_id, pair_name, ok, dry_run, status, started_at, ended_at, result_json) "
            "VALUES (1, 'Archiv', 1, 0, 'ok', ?, ?, ?)",
            (
                oct_5,
                oct_5 + 60,
                json.dumps(
                    {
                        "remote": "b2:archive",
                        "direction": "pull",
                        "transferred": "6 GiB",
                    }
                ),
            ),
        )
        # Previous month push: ignored
        conn.execute(
            "INSERT INTO pair_runs(job_id, pair_name, ok, dry_run, status, started_at, ended_at, result_json) "
            "VALUES (2, 'Fotos', 1, 0, 'ok', ?, ?, ?)",
            (
                sep_25,
                sep_25 + 60,
                json.dumps(
                    {
                        "remote": "b2:photos",
                        "direction": "push",
                        "transferred": "50 GiB",
                    }
                ),
            ),
        )
        # Current month restoretest + bitrot_scrub jobs: 3 GiB + 2 GiB = 5 GiB egress
        conn.execute(
            "INSERT INTO jobs(id, kind, status, started_at, ended_at, dry_run, summary_json) "
            "VALUES (3, 'restoretest', 'ok', ?, ?, 0, ?)",
            (oct_5, oct_5 + 30, json.dumps({"total_bytes": 3 * GIB})),
        )
        conn.execute(
            "INSERT INTO jobs(id, kind, status, started_at, ended_at, dry_run, summary_json) "
            "VALUES (4, 'bitrot_scrub', 'ok', ?, ?, 0, ?)",
            (oct_5, oct_5 + 30, json.dumps({"checked_bytes": 2 * GIB})),
        )

    config = {
        "backup": {
            "timezone": "Europe/Berlin",
            "transfer_budget": {
                "enabled": True,
                "monthly_egress_gb": 10,
                "monthly_upload_gb": 10,
                "warn_percent": 80,
                "throttle_optional_jobs": True,
            },
        }
    }

    usage = cloud_quota.monthly_transfer_usage(db, config, now=NOW)
    # Egress: 6 GiB (pull) + 3 GiB (restoretest) + 2 GiB (bitrot_scrub) = 11 GiB (110% of 10 GiB)
    # Upload: 8 GiB (80% of 10 GiB)
    assert usage["egress_bytes"] == 11 * GIB
    assert usage["upload_bytes"] == 8 * GIB
    assert usage["egress_percent"] == 110.0
    assert usage["upload_percent"] == 80.0
    assert usage["egress_warning"] is True
    assert usage["egress_exceeded"] is True
    assert usage["upload_warning"] is True
    assert usage["upload_exceeded"] is False
    assert usage["throttle_optional_active"] is True
    assert usage["by_remote"]["b2"] == {
        "upload_bytes": 8 * GIB,
        "egress_bytes": 6 * GIB,
    }

    throttled, reason = cloud_quota.should_throttle_optional_job(db, config, now=NOW)
    assert throttled is True
    assert "Egress-Budget" in reason

    config["backup"]["transfer_budget"]["throttle_optional_jobs"] = False
    throttled_off, reason_off = cloud_quota.should_throttle_optional_job(
        db, config, now=NOW
    )
    assert throttled_off is False
    assert reason_off == ""


def test_quota_report_cache_and_api_endpoint(tmp_path: Path, monkeypatch):
    cloud_quota.clear_quota_cache()
    db = Database(tmp_path / "api_quota.db")
    config = {
        "backup": {
            "timezone": "Europe/Berlin",
            "pairs": [{"name": "Fotos", "local": "/data/fotos", "remote": "b2:fotos"}],
            "transfer_budget": {"enabled": True, "monthly_egress_gb": 20},
        }
    }
    call_count = 0

    def fake_run(_cmd, **_kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeCompleted(
            stdout=json.dumps({"total": 50 * GIB, "used": 10 * GIB, "free": 40 * GIB}),
            returncode=0,
        )

    first = cloud_quota.quota_report(config, db, refresh=False, run=fake_run, now=NOW)
    second = cloud_quota.quota_report(
        config, db, refresh=False, run=fake_run, now=NOW + 60
    )
    assert call_count == 1
    assert first["remotes"][0]["remote"] == "b2"
    assert second["remotes"][0]["percent_used"] == 20.0

    third = cloud_quota.quota_report(
        config, db, refresh=True, run=fake_run, now=NOW + 120
    )
    assert call_count == 2
    assert third["remotes"][0]["free_bytes"] == 40 * GIB

    monkeypatch.setattr(
        api_quota, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    monkeypatch.setattr(api_quota, "get_db", lambda: db)
    monkeypatch.setattr(
        cloud_quota,
        "remote_about",
        lambda name, **_kw: {
            "remote": name,
            "ok": True,
            "total_bytes": 50 * GIB,
            "used_bytes": 10 * GIB,
            "free_bytes": 40 * GIB,
            "trashed_bytes": None,
            "other_bytes": None,
            "percent_used": 20.0,
            "error": None,
            "measured_at": NOW,
        },
    )

    app = FastAPI()
    app.include_router(api_quota.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app)

    resp = client.get("/api/storage/quota?refresh=true")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["remotes"]) == 1
    assert body["remotes"][0]["remote"] == "b2"
    assert body["budget"]["monthly_egress_gb"] == 20
