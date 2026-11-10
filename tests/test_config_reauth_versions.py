"""F02: Versions-Ausdünnung und Versionsablage nur mit aktuellem Passwort lockern."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException, Request

from app.config_store import Config
from app.routes import api_config

LIFECYCLE = {"enabled": True, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 12}


def _config(tmp_path: Path) -> dict:
    return {
        "web": {
            "username": "admin",
            "secret_key": "server-owned-secret-key-that-is-long-enough",
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "tmp"),
        },
        "backup": {
            "default_schedule": "manual",
            "backup_dir": ".versions/{date}",
            "versions_lifecycle": dict(LIFECYCLE),
            "pairs": [
                {
                    "name": "Spiegel",
                    "remote": "wasabi:media-bk",
                    "local": str(tmp_path / "media"),
                    "direction": "push",
                    "mode": "sync",
                    "schedule": "manual",
                    "backup_dir": ".v/{date}",
                }
            ],
        },
        "notifications": {"webhooks": []},
    }


def _changed(tmp_path: Path, edit) -> bool:
    old = _config(tmp_path)
    new = copy.deepcopy(old)
    edit(new["backup"], new["backup"]["pairs"][0])
    return api_config._sensitive_config_changed(old, new)


@pytest.mark.parametrize(
    "edit",
    [
        lambda b, p: b["versions_lifecycle"].update(keep_daily=3),
        lambda b, p: p.update(versions_lifecycle={**LIFECYCLE, "keep_monthly": 1}),
        lambda b, p: b.update(backup_dir="archiv/{date}"),
        lambda b, p: p.update(backup_dir=".andere/{date}"),
        lambda b, p: p.update(backup_dir2="/srv/alt/{date}"),
        lambda b, p: p.update(backup_dir=""),
    ],
    ids=[
        "global-senken",
        "pair-override-kleiner",
        "global-dir",
        "pair-dir",
        "pair-dir2",
        "pair-dir-leer",
    ],
)
def test_weaker_versions_or_other_backup_dir_is_sensitive(tmp_path, edit):
    assert _changed(tmp_path, edit) is True


@pytest.mark.parametrize(
    "edit",
    [
        lambda b, p: b["versions_lifecycle"].update(keep_weekly=8, keep_daily=30),
        lambda b, p: b["versions_lifecycle"].update(enabled=False),
        lambda b, p: p.update(versions_lifecycle=dict(LIFECYCLE)),
        lambda b, p: p.update(schedule="0 2 * * *"),
    ],
    ids=["erhoehen", "abschalten", "pair-override-gleich", "fremdes-feld"],
)
def test_stronger_or_unrelated_changes_need_no_password(tmp_path, edit):
    assert _changed(tmp_path, edit) is False


def test_enabling_and_new_pairs_are_measured_against_global(tmp_path):
    old = _config(tmp_path)
    old["backup"]["versions_lifecycle"]["enabled"] = False
    new = copy.deepcopy(old)
    new["backup"]["versions_lifecycle"]["enabled"] = "true"
    assert api_config._sensitive_config_changed(old, new) is True

    new = copy.deepcopy(old)
    new["backup"]["pairs"][0]["versions_lifecycle"] = {"enabled": True}
    assert api_config._sensitive_config_changed(old, new) is True

    new = copy.deepcopy(old)
    pair = {**old["backup"]["pairs"][0], "name": "Neu", "local": str(tmp_path / "neu")}
    del pair["backup_dir"]  # erbt global
    new["backup"]["pairs"].append(pair)
    assert api_config._sensitive_config_changed(old, new) is False
    pair["versions_lifecycle"] = {"enabled": True, "keep_daily": 0}
    assert api_config._sensitive_config_changed(old, new) is True
    del pair["versions_lifecycle"]
    pair["backup_dir"] = "/tmp/fest"
    assert api_config._sensitive_config_changed(old, new) is True


def test_changed_pair_id_cannot_hide_backup_dir_change(tmp_path):
    old = _config(tmp_path)
    old["backup"]["pairs"][0]["id"] = "a" * 32
    new = copy.deepcopy(old)
    new["backup"]["pairs"][0].update(id="b" * 32, backup_dir="/tmp/fest")
    assert api_config._sensitive_config_changed(old, new) is True


class _AuditDB:
    def audit_add(self, *_args, **_kwargs) -> None:
        return None


def test_route_requires_password_for_aggressive_lifecycle(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = _config(tmp_path)
    original["backup"]["versions_lifecycle"]["enabled"] = False
    path.write_text(yaml.safe_dump(original), encoding="utf-8")
    store = Config(path)
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    candidate["backup"]["versions_lifecycle"].update(enabled=True, keep_daily=0)
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: _AuditDB())
    request = Request(
        {"type": "http", "method": "PUT", "path": "/api/config", "headers": []}
    )

    with pytest.raises(HTTPException) as raised:
        api_config.update_config(
            request, api_config.ConfigUpdate(config=candidate), user="admin"
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["reauth_required"] is True
    assert store.get("backup", "versions_lifecycle", "enabled") is False
