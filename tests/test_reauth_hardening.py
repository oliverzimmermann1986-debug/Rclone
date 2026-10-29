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


@pytest.mark.parametrize("native", [False, True])
def test_registration_cannot_rebind_old_password_proof_to_new_generation(
    tmp_path, monkeypatch, native
):
    from app.routes import api_webauthn

    store, database = _setup(tmp_path, monkeypatch)
    store.update(
        lambda data: data["web"].update(
            webauthn_rp_id="backup.example",
            webauthn_origin="https://backup.example",
            secure_cookie=True,
        )
    )
    token = auth.create_session("admin")
    reauthenticate = api_webauthn.require_reauthentication

    def rotate_after_proof(*args):
        proof = reauthenticate(*args)
        store.update(lambda data: data["web"].update(session_version=2))
        return proof

    monkeypatch.setattr(api_webauthn, "require_reauthentication", rotate_after_proof)
    with pytest.raises(HTTPException) as denied:
        if native:
            api_webauthn.create_native_registration_ticket(
                api_webauthn.NativeRegistrationTicketRequest(
                    method="passkey",
                    current_password="correct-password",
                    app_challenge="C" * 43,
                ),
                _request(token),
                user="admin",
            )
        else:
            api_webauthn.begin_registration(
                api_webauthn.RegistrationOptionsRequest(
                    method="passkey", current_password="correct-password"
                ),
                _request(token),
                user="admin",
            )
    assert denied.value.status_code in {400, 401}
    with database.conn() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM webauthn_challenges").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM native_webauthn_registrations"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "scope",
    [
        "global",
        "pair",
        "remove_pair",
        "change_identity",
        "remove_copy_pair",
        "remove_backup",
    ],
)
def test_quarantine_controls_cannot_be_weakened_without_password(
    tmp_path, monkeypatch, scope
):
    from app import protection

    store, database = _setup(tmp_path, monkeypatch)
    pair = {
        "id": "a" * 32,
        "name": "Protected",
        "local": str(tmp_path / "source"),
        "remote": "cloud:/backup",
        "direction": "push",
        "mode": "sync",
        "allow_delete": True,
        "max_delete": 10,
    }
    if scope == "remove_copy_pair":
        pair["mode"] = "copy"
    store.update(lambda data: data["backup"].update(pairs=[pair], filter_file=""))
    database.runtime_set(
        protection._ANOMALY_STATE_KEY,
        {"quarantines": {protection._state_key(pair): {"pair": "Protected"}}},
    )
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    if scope == "global":
        candidate["backup"]["anomaly_guard"] = {"enabled": False}
    elif scope == "pair":
        candidate["backup"]["pairs"][0]["anomaly_guard"] = {"enabled": False}
    elif scope == "remove_backup":
        del candidate["backup"]
    elif scope in {"remove_pair", "remove_copy_pair"}:
        candidate["backup"]["pairs"] = []
    else:
        candidate["backup"]["pairs"][0]["id"] = "b" * 32
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            _request("config-session"),
            api_config.ConfigUpdate(config=candidate),
            user="admin",
        )
    assert denied.value.status_code == 403
    assert store.revision == revision


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


@pytest.mark.parametrize("pairs", [True, 7, {"unexpected": "mapping"}])
def test_quarantine_security_check_preserves_config_validation_errors(
    tmp_path, monkeypatch, pairs
):
    store, _database = _setup(tmp_path, monkeypatch)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["backup"]["pairs"] = pairs
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            _request("config-session"),
            api_config.ConfigUpdate(config=candidate),
            user="admin",
        )
    assert denied.value.status_code == 422
    assert store.revision == revision


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
        _request(auth.create_session("admin")),
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


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("paths", "data_dir", "alternate-data"),
        ("paths", "recovery_dir", "alternate-recovery"),
        ("backup", "filter_file", "alternate-filters.txt"),
    ],
)
def test_path_changes_require_password(tmp_path, monkeypatch, section, key, value):
    store, _ = _setup(tmp_path, monkeypatch)
    candidate, revision = store.snapshot_with_revision()
    candidate[section][key] = str(
        (tmp_path / "data" if section == "backup" else tmp_path) / value
    )
    candidate["_revision"] = revision
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            _request("path-change"),
            api_config.ConfigUpdate(config=candidate),
            user="admin",
        )
    assert denied.value.status_code == 403
    assert store.revision == revision


def test_omitted_sensitive_fields_are_preserved_in_saved_config(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)

    def customize(data):
        data["web"].update({"secure_cookie": True, "hsts_seconds": 1234})
        data["paths"]["recovery_dir"] = str(tmp_path / "private-recovery")
        data["notifications"]["apns"] = {"key_file": str(tmp_path / "data" / "key.p8")}

    store.update(customize)
    candidate, revision = store.snapshot_with_revision()
    candidate["web"].pop("secure_cookie")
    candidate["web"].pop("hsts_seconds")
    candidate["paths"].pop("recovery_dir")
    candidate["notifications"].pop("apns")
    candidate["backup"]["max_parallel"] = 3
    candidate["_revision"] = revision
    result = api_config.update_config(
        _request("legacy-client"),
        api_config.ConfigUpdate(config=candidate),
        user="admin",
    )
    assert result["ok"]
    assert store.get("web", "secure_cookie") is True
    assert store.get("web", "hsts_seconds") == 1234
    assert store.get("paths", "recovery_dir") == str(tmp_path / "private-recovery")
    assert store.get("notifications", "apns", "key_file") == str(
        tmp_path / "data" / "key.p8"
    )


def test_apns_key_change_requires_password(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)
    candidate, revision = store.snapshot_with_revision()
    candidate["notifications"]["apns"] = {
        "key_file": str(tmp_path / "data" / "other.p8")
    }
    candidate["_revision"] = revision
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            _request("key-change"),
            api_config.ConfigUpdate(config=candidate),
            user="admin",
        )
    assert denied.value.status_code == 403
    assert store.revision == revision


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
