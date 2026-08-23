from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import bcrypt
import pytest
import yaml
from fastapi import HTTPException, Request

from app import __version__ as app_version, config_store, db
from app.config_store import Config
from app.db import Database
from app.routes import api_maintenance


def _recovery_config(tmp_path: Path, password: str = "test-password-long") -> dict:
    return {
        "web": {
            "username": "admin",
            "password": "",
            "password_hash": bcrypt.hashpw(
                password.encode(), bcrypt.gensalt(rounds=4)
            ).decode(),
            "secret_key": "support-secret-must-never-leak-123456789",
            "session_version": 1,
            "allowed_hosts": ["testserver"],
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "tmp"),
        },
        "backup": {
            "enabled": True,
            "pairs": [],
            "default_schedule": "manual",
            "max_parallel": 2,
            "filter_file": str(tmp_path / "data" / "rclone-filters.txt"),
        },
        "notifications": {
            "custom_delivery": {
                "token": "CANARY_TOKEN_MUST_NOT_LEAK",
                "endpoint": "cloud:https://user:CANARY_PASS@example.test",
            }
        },
    }


def _setup(tmp_path: Path, monkeypatch) -> tuple[Config, Database]:
    for name in ("data", "logs", "tmp"):
        (tmp_path / name).mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_recovery_config(tmp_path), allow_unicode=True),
        encoding="utf-8",
    )
    store = Config(config_path)
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    return store, database


def _request(token: str = "recovery-test-session") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/maintenance/config/snapshots/restore",
            "headers": [
                (b"cookie", f"rclone_sync_session={token}".encode("ascii"))
            ],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_snapshot_restore_keeps_identity_and_invalidates_sessions(
    tmp_path, monkeypatch
):
    store, _database = _setup(tmp_path, monkeypatch)
    original_hash = store.get("web", "password_hash")

    created = api_maintenance.create_config_snapshot()
    snapshot = created["snapshot"]
    snapshot_path = tmp_path / "data" / "config-snapshots" / snapshot["name"]
    assert snapshot_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    assert len(snapshot["sha256"]) == 64

    def mutate(data):
        data["backup"]["max_parallel"] = 9
        data["web"]["password_hash"] = bcrypt.hashpw(
            b"test-password-long", bcrypt.gensalt(rounds=4)
        ).decode()

    store.update(mutate)
    current_hash = store.get("web", "password_hash")
    assert current_hash != original_hash
    revision = store.revision

    with pytest.raises(HTTPException) as mismatch:
        api_maintenance.restore_config_snapshot(
            _request(),
            api_maintenance.SnapshotRestore(
                name=snapshot["name"],
                current_password="test-password-long",
                expected_revision=revision,
                sha256="0" * 64,
            ),
            user="admin",
        )
    assert mismatch.value.status_code == 409

    restored = api_maintenance.restore_config_snapshot(
        _request(),
        api_maintenance.SnapshotRestore(
            name=snapshot["name"],
            current_password="test-password-long",
            expected_revision=revision,
            sha256=snapshot["sha256"],
        ),
        user="admin",
    )

    assert restored["ok"] is True
    assert restored["reauthenticate"] is True
    assert store.get("backup", "max_parallel") == 2
    assert store.get("web", "password_hash") == current_hash
    assert store.get("web", "session_version") == 2
    names = [
        entry["name"] for entry in api_maintenance.list_config_snapshots()["snapshots"]
    ]
    assert snapshot["name"] in names
    assert any(name.startswith("pre-restore-") for name in names)


def test_support_bundle_is_redacted_and_contains_diagnostics(tmp_path, monkeypatch):
    store, database = _setup(tmp_path, monkeypatch)
    job_id = database.job_start("backup")
    database.job_finish(job_id, "error", {"error": "diagnostic failure"})
    (tmp_path / "logs" / "job.log").write_text("not bundled", encoding="utf-8")

    response = api_maintenance.support_bundle()
    assert response.media_type == "application/zip"
    assert "attachment;" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "config-redacted.yaml",
            "diagnostics.json",
        }
        redacted = archive.read("config-redacted.yaml").decode("utf-8")
        diagnostics = json.loads(archive.read("diagnostics.json"))

    assert store.get("web", "secret_key") not in redacted
    assert "CANARY_TOKEN_MUST_NOT_LEAK" not in redacted
    assert "CANARY_PASS" not in redacted
    assert "***REDACTED***" in redacted
    assert diagnostics["app_version"] == app_version
    assert diagnostics["database"]["integrity"]["ok"] is True
    assert diagnostics["recent_jobs"][0]["id"] == job_id
    assert diagnostics["log_inventory"]["logs"][0]["path"] == "job.log"
    assert "not bundled" not in response.body.decode("latin-1", errors="ignore")
