"""Tests für die Proxmox-Backup-Server-Integration (v1.8.0)."""

import time
from pathlib import Path

import pytest
import yaml

from app import config_store
from app.config_validation import ConfigValidationError, validate_config
from app.config_store import Config
from app.db import Database
from app.jobs import pbs_backup, scheduler


def _base_config(tmp_path: Path, pbs: dict) -> dict:
    return {
        "web": {
            "username": "admin",
            "password": "very-secure-password",
            "secret_key": "test-secret-which-is-long-enough-123456789",
        },
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path),
        },
        "backup": {"enabled": True, "pairs": []},
        "pbs": pbs,
    }


def _install_config(tmp_path: Path, monkeypatch, pbs: dict) -> Config:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_base_config(tmp_path, pbs)), encoding="utf-8"
    )
    store = Config(config_path)
    monkeypatch.setattr(config_store, "_config", store)
    return store


def test_validation_normalizes_and_rejects(tmp_path):
    cfg, warnings = validate_config(
        _base_config(
            tmp_path,
            {
                "enabled": True,
                "repository": "backup@pbs!tok@10.0.0.5:store1",
                "password": "secret",
                "targets": [
                    {"name": "docs", "paths": "/mnt/a\n/mnt/b", "schedule": "0 2 * * *"}
                ],
            },
        )
    )
    target = cfg["pbs"]["targets"][0]
    assert target["paths"] == ["/mnt/a", "/mnt/b"]
    assert cfg["pbs"]["timeout_hours"] == 4

    with pytest.raises(ConfigValidationError):
        validate_config(
            _base_config(
                tmp_path,
                {
                    "enabled": True,
                    "repository": "x@y@z:s",
                    "targets": [{"name": "bad", "paths": ["relativ"], "schedule": "*"}],
                },
            )
        )


def test_build_backup_and_prune_commands(monkeypatch):
    monkeypatch.setattr(pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    settings = {
        "repository": "backup@pbs!tok@host:store",
        "namespace": "ns1",
        "backup_id": "lxc203",
        "keep": {"keep_daily": 7, "keep_weekly": 0},
    }
    target = {"name": "docs", "paths": ["/mnt/nas/dokumente", "/mnt/nas/fotos"]}
    backup_id = pbs_backup._target_backup_id(settings, target)
    assert backup_id == "lxc203"
    cmd = pbs_backup.build_backup_command(settings, target)
    assert cmd[:2] == ["/usr/bin/pbc", "backup"]
    assert "dokumente.pxar:/mnt/nas/dokumente" in cmd
    assert "fotos.pxar:/mnt/nas/fotos" in cmd
    assert cmd[cmd.index("--repository") + 1] == "backup@pbs!tok@host:store"
    assert cmd[cmd.index("--ns") + 1] == "ns1"
    assert cmd[cmd.index("--backup-id") + 1] == backup_id

    prune = pbs_backup.build_prune_command(settings, target)
    assert prune is not None
    assert prune[1:3] == ["prune", f"host/{backup_id}"]
    assert prune[prune.index("--keep-daily") + 1] == "7"
    assert "--keep-weekly" not in prune


def test_validation_rejects_ambiguous_multi_target_backup_ids(tmp_path):
    base = _base_config(
        tmp_path,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "targets": [
                {"name": "docs", "paths": ["/mnt/docs"]},
                {"name": "photos", "paths": ["/mnt/photos"]},
            ],
        },
    )
    with pytest.raises(ConfigValidationError, match="backup_id.*explizit"):
        validate_config(base)

    base["pbs"]["targets"][0]["backup_id"] = "shared"
    base["pbs"]["targets"][1]["backup_id"] = "SHARED"
    with pytest.raises(ConfigValidationError, match="backup_id.*nicht eindeutig"):
        validate_config(base)


def test_validation_accepts_distinct_multi_target_backup_ids(tmp_path):
    normalized, _ = validate_config(
        _base_config(
            tmp_path,
            {
                "enabled": True,
                "repository": "backup@pbs!tok@host:store",
                "targets": [
                    {
                        "name": "docs",
                        "paths": ["/mnt/docs"],
                        "backup_id": "docs",
                    },
                    {
                        "name": "photos",
                        "paths": ["/mnt/photos"],
                        "backup_id": "photos",
                    },
                ],
            },
        )
    )
    assert [target["backup_id"] for target in normalized["pbs"]["targets"]] == [
        "docs",
        "photos",
    ]


def test_validation_rejects_case_insensitive_target_names_and_invalid_ids(tmp_path):
    base = _base_config(
        tmp_path,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "backup_id": "../invalid",
            "targets": [
                {
                    "name": "Docs",
                    "paths": ["/mnt/docs"],
                    "backup_id": "docs",
                },
                {
                    "name": "docs",
                    "paths": ["/mnt/docs-copy"],
                    "backup_id": "bad/id",
                },
            ],
        },
    )
    with pytest.raises(ConfigValidationError) as raised:
        validate_config(base)
    message = str(raised.value)
    assert "name ist doppelt" in message
    assert "pbs.backup_id" in message
    assert "pbs.targets[1].backup_id" in message


def test_validation_rejects_pbs_path_outside_mountpoint(tmp_path):
    mountpoint = tmp_path / "mounted"
    outside = tmp_path / "outside"
    base = _base_config(
        tmp_path,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "targets": [
                {
                    "name": "docs",
                    "paths": [str(outside)],
                    "require_mountpoint": True,
                    "mountpoint": str(mountpoint),
                }
            ],
        },
    )
    with pytest.raises(ConfigValidationError, match="mountpoint.*PBS-Pfade"):
        validate_config(base)


def test_run_pbs_backup_records_targets(tmp_path, monkeypatch):
    data = tmp_path / "quelle"
    data.mkdir()
    (data / "payload.txt").write_text("data", encoding="utf-8")
    _install_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "password": "s3cret",
            "targets": [
                {"name": "docs", "paths": [str(data)], "schedule": "manual"},
            ],
        },
    )
    monkeypatch.setattr(pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    captured = {}

    def fake_run(cmd, log_file, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("extra_env")
        Path(log_file).write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(pbs_backup, "_run_rclone_command", fake_run)
    monkeypatch.setattr(pbs_backup, "reset_cancel", lambda *_args: None)
    monkeypatch.setattr(pbs_backup, "is_cancelled", lambda *_args: False)
    monkeypatch.setattr(pbs_backup, "_notify_result", lambda summary: None)

    summary = pbs_backup.run_pbs_backup(trigger="web")
    assert summary["ok"], summary
    assert summary["pairs"][0]["name"] == "pbs:docs"
    assert captured["env"]["PBS_PASSWORD"] == "s3cret"

    # Fehlender Pfad (Mount weg) => Fehler statt Leerbackup
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            _base_config(
                tmp_path,
                {
                    "enabled": True,
                    "repository": "backup@pbs!tok@host:store",
                    "password": "s3cret",
                    "targets": [
                        {"name": "weg", "paths": [str(tmp_path / "fehlt")]},
                    ],
                },
            )
        ),
        encoding="utf-8",
    )
    config_store._config = Config(tmp_path / "config.yaml")
    summary = pbs_backup.run_pbs_backup(trigger="web")
    assert not summary["ok"]
    assert "nicht vorhanden" in summary["pairs"][0]["error"]


def test_pbs_source_guard_rejects_unmounted_path(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("data", encoding="utf-8")
    _install_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "targets": [
                {
                    "name": "guarded",
                    "paths": [str(source)],
                    "require_mountpoint": True,
                    "min_files": 1,
                }
            ],
        },
    )
    monkeypatch.setattr(pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    monkeypatch.setattr(pbs_backup.os.path, "ismount", lambda _path: False)
    monkeypatch.setattr(pbs_backup, "reset_cancel", lambda *_args: None)
    monkeypatch.setattr(pbs_backup, "is_cancelled", lambda *_args: False)
    monkeypatch.setattr(pbs_backup, "_notify_result", lambda _summary: None)
    monkeypatch.setattr(
        pbs_backup,
        "_run_rclone_command",
        lambda *_args, **_kwargs: pytest.fail("Backup darf nicht gestartet werden"),
    )

    summary = pbs_backup.run_pbs_backup(trigger="web")

    assert summary["ok"] is False
    assert "nicht eingehängt" in summary["pairs"][0]["error"]


def test_prune_failure_marks_pbs_run_degraded(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("data", encoding="utf-8")
    _install_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "keep": {"keep_daily": 7},
            "targets": [{"name": "docs", "paths": [str(source)], "min_files": 1}],
        },
    )
    monkeypatch.setattr(pbs_backup, "client_path", lambda: "/usr/bin/pbc")
    monkeypatch.setattr(pbs_backup, "reset_cancel", lambda *_args: None)
    monkeypatch.setattr(pbs_backup, "is_cancelled", lambda *_args: False)
    monkeypatch.setattr(pbs_backup, "_notify_result", lambda _summary: None)
    returncodes = iter((0, 2))
    monkeypatch.setattr(
        pbs_backup,
        "_run_rclone_command",
        lambda *_args, **_kwargs: next(returncodes),
    )

    summary = pbs_backup.run_pbs_backup(trigger="scheduler")

    assert summary["ok"] is False
    assert summary["pairs"][0]["degraded"] is True
    assert summary["pairs"][0]["prune_ok"] is False


def test_find_due_pbs_targets_uses_pair_runs(tmp_path, monkeypatch):
    cfg = _install_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "repository": "backup@pbs!tok@host:store",
            "password": "x",
            "targets": [
                {"name": "docs", "paths": ["/mnt/a"], "schedule": "*/5 * * * *"},
                {"name": "manuell", "paths": ["/mnt/b"], "schedule": "manual"},
            ],
        },
    )
    monkeypatch.setattr("app.jobs.pbs_backup.client_path", lambda: "/usr/bin/pbc")
    db = Database(tmp_path / "app.db")

    # Erfolg vor 1h -> */5-Plan ist fällig; "manual" nie.
    job_id = db.job_start("pbs")
    db.job_finish(
        job_id,
        "ok",
        {
            "ok": True,
            "pairs": [{"name": "pbs:docs", "ok": True, "trigger": "scheduler"}],
        },
    )
    # job_finish stempelt mit "jetzt" — für den Test eine Stunde zurückdatieren.
    with db.conn() as connection:
        connection.execute(
            "UPDATE pair_runs SET started_at=?, ended_at=? WHERE pair_name='pbs:docs'",
            (time.time() - 3600, time.time() - 3590),
        )
    due, status = scheduler.find_due_pbs_targets(cfg, db)
    assert due == ["docs"]
    assert {item["name"] for item in status} == {"docs", "manuell"}

    # Deaktiviert -> nichts fällig
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(_base_config(tmp_path, {"enabled": False, "targets": []})),
        encoding="utf-8",
    )
    config_store._config = Config(tmp_path / "config.yaml")
    due, status = scheduler.find_due_pbs_targets(config_store._config, db)
    assert due == [] and status == []


def test_local_to_local_pair_and_overlap_rejected(tmp_path):
    base = _base_config(tmp_path, {"enabled": False, "targets": []})
    base["backup"]["pairs"] = [
        {
            "name": "usb",
            "remote": "/mnt/nas/fotos",
            "local": "/mnt/usb1/fotos",
            "direction": "push",
            "mode": "copy",
        }
    ]
    cfg, _ = validate_config(base)
    assert cfg["backup"]["pairs"][0]["remote"] == "/mnt/nas/fotos"

    base["backup"]["pairs"][0]["local"] = "/mnt/nas/fotos/unterordner"
    with pytest.raises(ConfigValidationError, match="ineinander"):
        validate_config(base)
