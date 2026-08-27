from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest
import yaml
from fastapi import HTTPException, Request

from app import auth, config_store, db
from app.config_store import Config
from app.db import Database
from app.routes import api_config, api_maintenance


def _config(tmp_path: Path) -> dict:
    return {
        "web": {
            "username": "admin",
            "password": "",
            "password_hash": bcrypt.hashpw(
                b"correct-password", bcrypt.gensalt(rounds=4)
            ).decode("ascii"),
            "secret_key": "reauth-test-secret-key-that-is-long-enough",
            "session_version": 1,
            "login_window_seconds": 300,
            "login_max_failures": 3,
            "login_lock_seconds": 60,
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
        "notifications": {"webhooks": []},
        "pbs": {"enabled": False, "targets": []},
    }


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Database]:
    for name in ("data", "logs", "tmp"):
        (tmp_path / name).mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config(tmp_path)), encoding="utf-8")
    store = Config(path)
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    return store, database


def _request(token: str, host: str = "192.0.2.10") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/config/change-password",
            "headers": [(b"cookie", f"rclone_sync_session={token}".encode("ascii"))],
            "client": (host, 12345),
        }
    )


def test_reauthentication_is_persistently_limited_per_session_and_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, database = _setup(tmp_path, monkeypatch)
    request = _request("session-secret-value")

    for _ in range(2):
        with pytest.raises(HTTPException) as denied:
            auth.require_reauthentication(request, "admin", "wrong-password")
        assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as blocked:
        auth.require_reauthentication(request, "admin", "wrong-password")
    assert blocked.value.status_code == 429
    assert int(blocked.value.headers["Retry-After"]) > 0
    assert blocked.value.detail["retry_after_seconds"] > 0

    # Selbst das richtige Passwort darf die laufende Sperre nicht umgehen.
    with pytest.raises(HTTPException) as still_blocked:
        auth.require_reauthentication(request, "admin", "correct-password")
    assert still_blocked.value.status_code == 429

    with database.conn() as connection:
        stored_keys = [
            str(row["client_key"])
            for row in connection.execute(
                "SELECT client_key FROM auth_failures ORDER BY client_key"
            ).fetchall()
        ]
    assert len(stored_keys) == 2
    assert all(key.startswith("reauth:") for key in stored_keys)
    assert all("session-secret-value" not in key for key in stored_keys)
    assert all("192.0.2.10" not in key for key in stored_keys)
    reopened = Database(database.path)
    assert reopened.auth_retry_after_many(auth.reauth_keys(request)) > 0


def test_success_clears_only_matching_reauthentication_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, database = _setup(tmp_path, monkeypatch)
    first = _request("session-a")
    second = _request("session-b")

    for request in (first, second):
        with pytest.raises(HTTPException):
            auth.require_reauthentication(request, "admin", "wrong-password")

    first_keys = set(auth.reauth_keys(first))
    second_keys = set(auth.reauth_keys(second))
    auth.require_reauthentication(first, "admin", "correct-password")

    with database.conn() as connection:
        remaining = {
            str(row["client_key"])
            for row in connection.execute("SELECT client_key FROM auth_failures")
        }
    assert not (first_keys & remaining)
    assert second_keys - first_keys <= remaining


class _FailingAuditDB:
    def audit_add(self, *_args, **_kwargs) -> None:
        raise OSError("audit unavailable")


class _FailingMarkerPath:
    def __truediv__(self, _name: str) -> "_FailingMarkerPath":
        return self

    def unlink(self, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise OSError("marker unavailable")


def test_password_change_stays_successful_when_post_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, database = _setup(tmp_path, monkeypatch)
    database.push_device_upsert("ab" * 32, "production")
    monkeypatch.setattr(api_config, "Path", lambda *_args: _FailingMarkerPath())
    monkeypatch.setattr(api_config, "get_db", lambda: _FailingAuditDB())

    result = api_config.change_password(
        _request("password-change"),
        api_config.PasswordChange(
            current_password="correct-password",
            new_password="new-correct-password",
        ),
        user="admin",
    )

    assert result["ok"] is True
    assert result["status"] == "success_with_warning"
    assert result["reauthenticate"] is True
    assert len(result["warnings"]) == 2
    assert bcrypt.checkpw(
        b"new-correct-password",
        store.get("web", "password_hash").encode("ascii"),
    )
    assert store.get("web", "session_version") == 2
    assert database.push_devices() == []


def test_config_update_stays_successful_when_post_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _database = _setup(tmp_path, monkeypatch)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["backup"]["max_parallel"] = 3
    monkeypatch.setattr(api_config, "get_db", lambda: _FailingAuditDB())

    result = api_config.update_config(
        _request("config-update"),
        api_config.ConfigUpdate(config=candidate),
        user="admin",
    )

    assert result["ok"] is True
    assert result["status"] == "success_with_warning"
    assert store.get("backup", "max_parallel") == 3
    assert any("Audit-Protokoll" in warning for warning in result["warnings"])


def test_snapshot_restore_stays_successful_when_post_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _database = _setup(tmp_path, monkeypatch)
    created = api_maintenance.create_config_snapshot()["snapshot"]

    def mutate(data: dict) -> None:
        data["backup"]["max_parallel"] = 9

    store.update(mutate)
    revision = store.revision
    monkeypatch.setattr(api_maintenance, "get_db", lambda: _FailingAuditDB())

    result = api_maintenance.restore_config_snapshot(
        _request("snapshot-restore"),
        api_maintenance.SnapshotRestore(
            name=created["name"],
            current_password="correct-password",
            expected_revision=revision,
            sha256=created["sha256"],
        ),
        user="admin",
    )

    assert result["ok"] is True
    assert result["status"] == "success_with_warning"
    assert result["reauthenticate"] is True
    assert store.get("backup", "max_parallel") == 2
    assert store.get("web", "session_version") == 2
    assert any("Audit-Protokoll" in warning for warning in result["warnings"])
