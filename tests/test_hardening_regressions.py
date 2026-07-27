from __future__ import annotations

import copy
from pathlib import Path

import bcrypt
import pytest
import yaml
from fastapi import HTTPException

from app.config_store import Config
from app.config_validation import ConfigValidationError, validate_config
from app.jobs.pair_planner import pairs_conflict
from app.routes import api_config, api_jobs, api_maintenance


def _config(tmp_path: Path) -> dict:
    return {
        "web": {
            "username": "admin",
            "password": "",
            "password_hash": bcrypt.hashpw(
                b"correct-password", bcrypt.gensalt(rounds=4)
            ).decode(),
            "secret_key": "server-owned-secret-key-that-is-long-enough",
            "session_version": 7,
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "tmp"),
        },
        "backup": {
            "enabled": True,
            "default_schedule": "manual",
            "pairs": [],
        },
        "notifications": {"webhooks": []},
        "pbs": {"enabled": False, "password": "pbs-canary", "targets": []},
    }


class _AuditDB:
    def audit_add(self, *_args, **_kwargs) -> None:
        return None


def test_config_write_failure_restores_memory_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"value": 1}), encoding="utf-8")
    store = Config(path)
    original_write = store._atomic_write_bytes

    def fail_target(destination: Path, raw: bytes) -> None:
        if destination == path:
            raise OSError("disk full")
        original_write(destination, raw)

    monkeypatch.setattr(store, "_atomic_write_bytes", fail_target)
    with pytest.raises(OSError, match="disk full"):
        store.replace({"value": 2})

    assert store.snapshot() == {"value": 1}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"value": 1}


def test_generic_config_save_cannot_replace_server_owned_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    original = _config(tmp_path)
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["web"].update(
        {
            "password": "attacker-password",
            "password_hash": bcrypt.hashpw(
                b"attacker-password", bcrypt.gensalt(rounds=4)
            ).decode(),
            "secret_key": "attacker-controlled-secret",
            "session_version": 1,
        }
    )
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())

    api_config.update_config(api_config.ConfigUpdate(config=candidate), user="admin")

    assert store.get("web", "password") == ""
    assert store.get("web", "password_hash") == original["web"]["password_hash"]
    assert store.get("web", "secret_key") == original["web"]["secret_key"]
    assert store.get("web", "session_version") == 7


def test_username_change_requires_reauthentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config(tmp_path)), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["web"]["username"] = "new-admin"
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())

    with pytest.raises(HTTPException) as raised:
        api_config.update_config(
            api_config.ConfigUpdate(config=candidate), user="admin"
        )
    assert raised.value.status_code == 403

    monkeypatch.setattr(api_config, "verify_password", lambda *_args: True)
    api_config.update_config(
        api_config.ConfigUpdate(config=candidate, current_password="correct-password"),
        user="admin",
    )
    assert store.get("web", "username") == "new-admin"
    assert store.get("web", "session_version") == 8


def test_string_false_cannot_bypass_sensitive_config_reauthentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config(tmp_path)), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["backup"]["require_delete_confirmation"] = "false"
    candidate["backup"]["require_max_delete_for_sync"] = "false"
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())

    with pytest.raises(HTTPException) as raised:
        api_config.update_config(
            api_config.ConfigUpdate(config=candidate), user="admin"
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["reauth_required"] is True


@pytest.mark.parametrize(
    ("field", "new_value"),
    (("namespace", "new"), ("backup_id", "docs-new")),
)
def test_pbs_target_route_change_requires_reauthentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    new_value: str,
) -> None:
    path = tmp_path / "config.yaml"
    original = _config(tmp_path)
    original["pbs"]["targets"] = [
        {
            "id": "a" * 32,
            "name": "docs",
            "paths": [str(tmp_path / "docs")],
            "namespace": "old",
            "backup_id": "docs-old",
        }
    ]
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["pbs"]["targets"][0][field] = new_value
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())

    with pytest.raises(HTTPException) as raised:
        api_config.update_config(
            api_config.ConfigUpdate(config=candidate), user="admin"
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["reauth_required"] is True


@pytest.mark.parametrize(
    ("field", "new_value"),
    (
        ("secure_cookie", False),
        ("hsts_seconds", 0),
        ("login_max_failures", 100),
        ("login_lock_seconds", 60),
    ),
)
def test_transport_and_lockout_changes_require_reauthentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    new_value: object,
) -> None:
    path = tmp_path / "config.yaml"
    original = _config(tmp_path)
    original["web"].update(
        {
            "secure_cookie": True,
            "hsts_seconds": 31536000,
            "login_max_failures": 10,
            "login_lock_seconds": 900,
        }
    )
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["web"][field] = new_value
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())

    with pytest.raises(HTTPException) as raised:
        api_config.update_config(
            api_config.ConfigUpdate(config=candidate), user="admin"
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["reauth_required"] is True


def test_absent_security_fields_are_not_treated_as_change() -> None:
    """Ein Client, der ein Feld nicht mitschickt, löst keine Reauth aus."""
    old = {"web": {"secure_cookie": False, "login_max_failures": 10}}
    new = {"web": {}}
    assert api_config._sensitive_config_changed(old, new) is False
    assert (
        api_config._sensitive_config_changed(old, {"web": {"login_max_failures": "10"}})
        is False
    )
    assert (
        api_config._sensitive_config_changed(old, {"web": {"login_max_failures": 50}})
        is True
    )


def test_bcrypt_byte_limit_is_reported_as_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_config, "verify_password", lambda *_args: True)
    with pytest.raises(HTTPException) as raised:
        api_config.change_password(
            api_config.PasswordChange(
                current_password="old-password", new_password="ä" * 37
            ),
            user="admin",
        )
    assert raised.value.status_code == 400
    assert "72 UTF-8-Bytes" in str(raised.value.detail)


def test_cron_is_exactly_five_fields_and_ids_are_stable(tmp_path: Path) -> None:
    invalid = _config(tmp_path)
    invalid["backup"]["default_schedule"] = "* * * * * *"
    with pytest.raises(ConfigValidationError):
        validate_config(invalid)

    valid = _config(tmp_path)
    valid["backup"]["pairs"] = [
        {
            "name": "docs",
            "remote": "cloud:docs",
            "local": str(tmp_path / "docs"),
            "direction": "pull",
            "mode": "copy",
        }
    ]
    normalized, _ = validate_config(valid)
    pair_id = normalized["backup"]["pairs"][0]["id"]
    again, _ = validate_config(normalized)
    assert again["backup"]["pairs"][0]["id"] == pair_id

    second_name = _config(tmp_path)
    second_name["backup"]["pairs"] = [
        valid["backup"]["pairs"][0],
        {**valid["backup"]["pairs"][0], "name": "docs-copy"},
    ]
    normalized, _ = validate_config(second_name)
    assert (
        normalized["backup"]["pairs"][0]["id"] != normalized["backup"]["pairs"][1]["id"]
    )


def test_local_destination_without_remote_file_guard_emits_warning(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["backup"]["pairs"] = [
        {
            "name": "local-copy",
            "remote": str(tmp_path / "target"),
            "local": str(tmp_path / "source"),
            "direction": "push",
            "mode": "copy",
            "min_local_files": 1,
            "min_remote_files": 0,
        }
    ]

    _normalized, warnings = validate_config(config)

    assert any("min_remote_files" in warning for warning in warnings)


def test_hidden_pair_options_and_reserved_names_are_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["backup"]["pairs"] = [
        {
            "name": "pbs:docs",
            "remote": "cloud:docs",
            "local": str(tmp_path / "docs"),
            "direction": "pull",
            "mode": "sync",
            "max_delete": 100,
            "options": {
                "max_delete": -1,
                "ignore_errors": True,
                "allow_unsafe_flags": True,
            },
        }
    ]
    with pytest.raises(ConfigValidationError) as raised:
        validate_config(config)
    message = str(raised.value)
    assert "pbs:" in message
    assert "options wird nicht mehr unterstützt" in message


def test_quicksync_validates_both_local_roots_and_nested_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Config:
        def get(self, *_keys, default=None):
            return [str(tmp_path)]

    monkeypatch.setattr(api_jobs, "get_config", lambda: _Config())
    payload = api_jobs.QuickSyncPayload(
        remote=str(tmp_path / "source"),
        local=str(tmp_path / "source" / "nested"),
        direction="push",
        mode="copy",
    )
    with pytest.raises(HTTPException) as raised:
        api_jobs._validate_quick_paths(payload)
    assert raised.value.status_code == 400
    assert "überlappen" in str(raised.value.detail)


def test_pair_conflicts_include_cross_role_resources(tmp_path: Path) -> None:
    shared = str(tmp_path / "shared")
    assert pairs_conflict(
        {"local": str(tmp_path / "a"), "remote": shared},
        {"local": shared, "remote": str(tmp_path / "b")},
    )


def test_maintenance_export_redacts_pbs_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _config(tmp_path)

    class _Config:
        def snapshot(self):
            return copy.deepcopy(snapshot)

    monkeypatch.setattr(api_maintenance, "get_config", lambda: _Config())
    exported = api_maintenance._redacted_export()
    assert exported["pbs"]["password"] == "***REDACTED***"
