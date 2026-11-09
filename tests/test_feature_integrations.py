"""Integration tests for Features 1, 2, 3, 4, and 6 across config, sync, and scheduler."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from app.config_validation import ConfigValidationError, validate_config
from app.db import Database
from app.jobs import bitrot_scrub, rclone_sync
from app.ransomware_guard import init_canary
from app.routes.api_config import _sensitive_config_changed


def _base_valid_config(tmp_path: Path) -> dict[str, Any]:
    src = tmp_path / "source"
    src.mkdir(parents=True, exist_ok=True)
    return {
        "schema_version": 2,
        "web": {
            "username": "admin",
            "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$abcdefghijklmnop$1234567890abcdef",
            "secret_key": secrets.token_hex(32),
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "tmp"),
            "recovery_dir": str(tmp_path / "recovery"),
            "recovery_snapshots_dir": str(tmp_path / "snapshots"),
            "device_vault_dir": str(tmp_path / "vault"),
        },
        "backup": {
            "ransomware_guard": {
                "enabled": True,
                "suspect_extensions": [".locked", ".akira"],
                "max_suspect_files": 1,
                "auto_lockdown": False,
            },
            "transfer_budget": {
                "enabled": True,
                "monthly_egress_gb": 10,
                "monthly_upload_gb": 50,
                "warn_percent": 80,
                "throttle_optional_jobs": True,
            },
            "pairs": [
                {
                    "name": "docs",
                    "local": str(src),
                    "remote": "cloud:docs",
                    "direction": "push",
                    "mode": "copy",
                    "sqlite_backup": True,
                    "canary_file": ".sicherpfad-canary.txt",
                }
            ],
        },
    }


def test_config_validation_and_sensitive_reauth(tmp_path: Path) -> None:
    raw = _base_valid_config(tmp_path)
    normalized, _ = validate_config(raw)
    assert normalized["backup"]["ransomware_guard"]["enabled"] is True
    assert normalized["backup"]["transfer_budget"]["monthly_egress_gb"] == 10
    pair = normalized["backup"]["pairs"][0]
    assert pair["sqlite_backup"] is True
    assert pair["canary_file"] == ".sicherpfad-canary.txt"

    # Weakening ransomware_guard or changing canary_file triggers reauthentication
    modified = _base_valid_config(tmp_path)
    modified["web"]["secret_key"] = normalized["web"]["secret_key"]
    modified["backup"]["ransomware_guard"]["enabled"] = False
    assert _sensitive_config_changed(normalized, modified) is True

    # Invalid sqlite_backup on pull direction is rejected
    bad = _base_valid_config(tmp_path)
    bad["backup"]["pairs"][0]["direction"] = "pull"
    with pytest.raises(ConfigValidationError):
        validate_config(bad)


def test_bitrot_scrub_throttled_by_egress_budget(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    cfg = _base_valid_config(tmp_path)
    cfg["backup"]["bitrot_scrub"] = {"enabled": True, "schedule": "* * * * *"}
    cfg["backup"]["transfer_budget"] = {
        "enabled": True,
        "monthly_egress_gb": 1,
        "monthly_upload_gb": 0,
        "warn_percent": 80,
        "throttle_optional_jobs": True,
    }
    # Record a restoretest job with 2 GiB egress in current month
    job_id = db.job_start("restoretest")
    db.job_finish(job_id, "ok", {"ok": True, "total_bytes": 2 * (1024**3)})

    due = bitrot_scrub.bitrot_scrub_due(cfg, db)
    assert due["due"] is False
    assert due["reason"] == "transfer_budget_throttled"


def test_sync_mounted_pair_blocks_on_tampered_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "sync.db")
    monkeypatch.setattr(rclone_sync, "get_db", lambda: db)
    monkeypatch.setattr("app.notifications.notify", lambda *a, **kw: None)

    src = tmp_path / "src_docs"
    src.mkdir()
    (src / "normal.txt").write_text("hello", encoding="utf-8")
    dst = tmp_path / "dst_docs"
    dst.mkdir()
    (dst / "seed.txt").write_text("seed", encoding="utf-8")

    pair = {
        "name": "docs",
        "local": str(src),
        "remote": str(dst),
        "direction": "push",
        "mode": "copy",
        "min_local_files": 1,
        "min_remote_files": 1,
    }
    canary = init_canary(pair, database=db)
    # Tamper with canary file
    (src / canary["canary_file"]).write_text(
        "ENCRYPTED_BY_RANSOMWARE", encoding="utf-8"
    )

    cfg = _base_valid_config(tmp_path)
    cfg["backup"]["pairs"] = [pair]
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    res = rclone_sync._sync_mounted_pair(
        pair,
        args=[],
        dry_run=False,
        timeout_sec=30,
        max_runtime_sec=60,
        log_dir=log_dir,
        run_id=1,
        config_snapshot=cfg,
        exclude_paths=(),
    )
    assert res["ok"] is False
    assert res.get("quarantined") is True
    assert "Canary" in str(res.get("error") or "")
