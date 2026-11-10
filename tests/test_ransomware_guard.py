from __future__ import annotations

import os
from pathlib import Path

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, protection, ransomware_guard
from app import config_store as config_store_module
from app import db as db_module
from app.db import Database
from app.routes import api_ransomware

PASSWORD = "Korrektes-Passwort-123!"


def _make_pair(local_dir: Path, name: str = "docs", **overrides) -> dict:
    base = {
        "name": name,
        "local": str(local_dir),
        "remote": "cloud:backup/docs",
        "direction": "push",
        "mode": "sync",
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_validate_canary_filename():
    assert (
        ransomware_guard.validate_canary_filename(".sicherpfad-canary.txt")
        == ".sicherpfad-canary.txt"
    )
    assert (
        ransomware_guard.validate_canary_filename("sub/canary.txt") == "sub/canary.txt"
    )

    for invalid in (
        "",
        "   ",
        "/abs/path.txt",
        "\\abs\\path.txt",
        "../escape.txt",
        "sub/../escape.txt",
        "C:canary.txt",
        "bad\x00name.txt",
        "bad\nname.txt",
        "a" * 256,
    ):
        with pytest.raises(ValueError):
            ransomware_guard.validate_canary_filename(invalid)


def test_ransomware_settings_defaults_and_overrides():
    backup = {
        "ransomware_guard": {
            "enabled": True,
            "suspect_extensions": ["LOCKED", ". custom "],
            "max_suspect_files": 5,
            "auto_lockdown": False,
        }
    }
    pair = {
        "name": "photos",
        "canary_file": " .my-canary ",
        "canary_sha256": " ABCDEF ",
        "ransomware_guard": {
            "max_suspect_files": 99_999,
            "auto_lockdown": True,
        },
    }
    settings = ransomware_guard.ransomware_settings(backup, pair)
    assert settings["enabled"] is True
    assert settings["suspect_extensions"] == [".locked", ".custom"]
    assert settings["max_suspect_files"] == 10_000
    assert settings["auto_lockdown"] is True
    assert settings["canary_file"] == ".my-canary"
    assert settings["canary_sha256"] == "abcdef"


def test_init_and_check_canary_lifecycle(tmp_path):
    database = Database(tmp_path / "app.db")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    pair = _make_pair(source_dir)

    unconfigured = ransomware_guard.check_canary(pair, database=database)
    assert unconfigured == {
        "configured": False,
        "ok": True,
        "reason": "not_configured",
    }

    initialized = ransomware_guard.init_canary(pair, database=database)
    assert initialized["ok"] is True
    assert initialized["canary_file"] == ransomware_guard.DEFAULT_CANARY_FILENAME
    assert len(initialized["canary_sha256"]) == 64

    canary_path = source_dir / ransomware_guard.DEFAULT_CANARY_FILENAME
    assert canary_path.is_file()

    checked = ransomware_guard.check_canary(pair, database=database)
    assert checked["configured"] is True
    assert checked["ok"] is True
    assert checked["reason"] == "ok"
    assert checked["actual_sha256"] == initialized["canary_sha256"]

    # Tamper with canary content
    canary_path.write_text("encrypted by ransomware", encoding="utf-8")
    tampered = ransomware_guard.check_canary(pair, database=database)
    assert tampered["configured"] is True
    assert tampered["ok"] is False
    assert tampered["reason"] == "hash_mismatch"
    assert "verändert" in tampered["error"]

    # Delete canary file
    canary_path.unlink()
    missing = ransomware_guard.check_canary(pair, database=database)
    assert missing["configured"] is True
    assert missing["ok"] is False
    assert missing["reason"] == "missing"
    assert "fehlt" in missing["error"]


def test_check_canary_detects_symlink(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    canary_link = source_dir / ".sicherpfad-canary.txt"
    try:
        os.symlink(outside, canary_link)
    except (OSError, NotImplementedError):
        canary_link.write_text("fallback", encoding="utf-8")
        orig_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda self: self == canary_link or orig_is_symlink(self),
        )

    pair = _make_pair(source_dir, canary_file=".sicherpfad-canary.txt")
    res = ransomware_guard.check_canary(pair)
    assert res["configured"] is True
    assert res["ok"] is False
    assert res["reason"] == "symlink"
    assert "symbolischer Link" in res["error"]


def test_preflight_blocks_on_tampered_canary(tmp_path):
    database = Database(tmp_path / "app.db")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    pair = _make_pair(source_dir)
    ransomware_guard.init_canary(pair, database=database)

    (source_dir / ransomware_guard.DEFAULT_CANARY_FILENAME).write_text(
        "corrupted", encoding="utf-8"
    )
    result = ransomware_guard.preflight_ransomware_guard(
        database,
        pair=pair,
        backup={"pairs": [pair], "ransomware_guard": {"enabled": False}},
        source=str(source_dir),
    )
    assert result["blocked"] is True
    assert result["reason"] == "canary_tampered"
    assert protection.has_quarantine(database, pair) is True


def test_scan_suspect_extensions_and_ignored_dirs(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "normal.txt").write_text("ok", encoding="utf-8")
    (source_dir / "sub").mkdir()
    (source_dir / "sub" / "victim.pdf.LOCKED").write_text("bad", encoding="utf-8")

    # Ignored internal directories must not trigger false positives
    versions_dir = source_dir / ".rclone-versions"
    versions_dir.mkdir()
    (versions_dir / "old.locked").write_text("ignored", encoding="utf-8")
    sqlite_dir = source_dir / ".sicherpfad-sqlite"
    sqlite_dir.mkdir()
    (sqlite_dir / "snap.enc").write_text("ignored", encoding="utf-8")

    scan = ransomware_guard.scan_suspect_extensions(
        str(source_dir),
        suspect_extensions=ransomware_guard.DEFAULT_SUSPECT_EXTENSIONS,
        max_suspect_files=1,
    )
    assert scan["scanned"] == 2
    assert scan["suspect_count"] == 1
    assert scan["suspect_files"] == ["sub/victim.pdf.LOCKED"]
    assert scan["blocked"] is True
    assert scan["truncated"] is False


def test_preflight_quarantines_and_triggers_auto_lockdown(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "secret.docx.ryuk").write_text("encrypted", encoding="utf-8")

    pair = _make_pair(source_dir)
    backup = {
        "pairs": [pair],
        "ransomware_guard": {
            "enabled": True,
            "max_suspect_files": 1,
            "auto_lockdown": True,
        },
    }

    lockdown_calls: list[tuple[str, str]] = []

    def fake_lockdown(*, actor: str, reason: str, **kwargs):
        lockdown_calls.append((actor, reason))
        return {"ok": True}

    monkeypatch.setattr(ransomware_guard, "activate_lockdown", fake_lockdown)

    result = ransomware_guard.preflight_ransomware_guard(
        database,
        pair=pair,
        backup=backup,
        source=str(source_dir),
    )
    assert result["blocked"] is True
    assert result["reason"] == "suspect_extensions"
    assert result["auto_lockdown_triggered"] is True
    assert len(lockdown_calls) == 1
    assert lockdown_calls[0][0] == "system"

    # Verify quarantine is visible to protection module
    quarantine = protection.active_quarantine(
        database, pair=pair, configured_pairs=[pair]
    )
    assert quarantine is not None
    assert quarantine["blocked"] is True
    assert quarantine["reason"] == "suspect_extensions"

    status = protection.anomaly_status(database, [pair])
    assert status["active"] == 1

    audits = database.audit_list(event_type="ransomware_blocked")
    assert len(audits) == 1


def test_api_ransomware_routes_and_reauth(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    monkeypatch.setattr(db_module, "_db", database)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("world", encoding="utf-8")

    pw_hash = bcrypt.hashpw(PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode(
        "ascii"
    )
    pair = _make_pair(source_dir, name="docs")
    raw_config = {
        "web": {
            "username": "admin",
            "password_hash": pw_hash,
            "secret_key": "x" * 64,
            "session_version": 1,
        },
        "backup": {
            "pairs": [pair],
            "ransomware_guard": {"enabled": True, "max_suspect_files": 1},
        },
    }

    class FakeConfig:
        def snapshot(self):
            return raw_config

        def get(self, *keys, default=None):
            cur = raw_config
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

    fake_cfg = FakeConfig()
    monkeypatch.setattr(config_store_module, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(auth, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(api_ransomware, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(api_ransomware, "get_db", lambda: database)

    app = FastAPI()
    app.include_router(api_ransomware.router)
    app.dependency_overrides[auth.require_auth] = lambda: "admin"
    app.dependency_overrides[api_ransomware.require_csrf] = lambda: None

    client = TestClient(app)

    # 1. Status before canary init
    res = client.get("/api/security/ransomware-status")
    assert res.status_code == 200
    data = res.json()
    assert data["settings"]["enabled"] is True
    assert data["pairs"][0]["name"] == "docs"
    assert data["pairs"][0]["canary"]["configured"] is False

    # 2. First canary init succeeds without password
    res = client.post("/api/security/canary/docs/init", json={})
    assert res.status_code == 200
    init_data = res.json()
    assert init_data["ok"] is True
    assert init_data["canary_file"] == ".sicherpfad-canary.txt"

    # 3. Re-initializing when already configured requires password
    res = client.post("/api/security/canary/docs/init", json={})
    assert res.status_code == 403
    assert res.json()["detail"]["reauth_required"] is True

    # Wrong password -> 403
    res = client.post(
        "/api/security/canary/docs/init",
        json={"current_password": "wrong-password"},
    )
    assert res.status_code == 403

    # Correct password -> 200
    res = client.post(
        "/api/security/canary/docs/init",
        json={"current_password": PASSWORD},
    )
    assert res.status_code == 200

    # 4. Verify endpoint when clean
    res = client.post("/api/security/canary/docs/verify")
    assert res.status_code == 200
    verify_data = res.json()
    assert verify_data["ok"] is True
    assert verify_data["canary"]["ok"] is True
    assert verify_data["extension_scan"]["blocked"] is False

    # 5. Verify endpoint detects suspect file
    (source_dir / "attack.locked").write_text("bad", encoding="utf-8")
    res = client.post("/api/security/canary/docs/verify")
    assert res.status_code == 200
    verify_bad = res.json()
    assert verify_bad["ok"] is False
    assert verify_bad["extension_scan"]["blocked"] is True
    assert verify_bad["extension_scan"]["suspect_count"] == 1
