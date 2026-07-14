from app.routes import api_jobs


def test_run_single_pair_forwards_dry_run(monkeypatch):
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    captured = {}

    def fake_run_backup(*, dry_run, pairs):
        captured.update(dry_run=dry_run, pairs=pairs)
        return {"ok": True}

    monkeypatch.setattr(api_jobs, "run_backup", fake_run_backup)
    assert api_jobs.run_single_pair("Fotos", dry_run=True) == {"ok": True}
    assert captured == {"dry_run": True, "pairs": "Fotos"}


def test_unsaved_pair_connection_test_uses_inline_draft(tmp_path, monkeypatch):
    import json
    import subprocess

    import bcrypt
    import yaml

    from app import config_store
    from app.config_store import Config
    from app.routes import api_test

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "password_hash": bcrypt.hashpw(
                        b"test-password-long", bcrypt.gensalt(rounds=4)
                    ).decode(),
                    "secret_key": "secret-key-that-is-long-enough-123456789",
                    "local_browse_roots": [str(tmp_path)],
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path / "logs"),
                    "temp_dir": str(tmp_path / "tmp"),
                },
                "backup": {"enabled": True, "pairs": [], "default_schedule": "manual"},
            }
        ),
        encoding="utf-8",
    )
    store = Config(config_path)
    monkeypatch.setattr(config_store, "_config", store)

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "listremotes":
            return subprocess.CompletedProcess(command, 0, "pcloud:\n", "")
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"count": 12, "bytes": 3456}), ""
        )

    monkeypatch.setattr(api_test.subprocess, "run", fake_run)
    draft = {
        "name": "Entwurf",
        "enabled": True,
        "remote": "pcloud:/noch-nicht-gespeichert",
        "local": str(tmp_path),
        "direction": "push",
        "mode": "copy",
        "schedule": "manual",
    }
    result = api_test.test_rclone(api_test.RcloneTest(pair=draft))

    assert result["ok"] is True
    assert result["tested_unsaved"] is True
    assert result["remote_path"] == draft["remote"]
    assert result["local_exists"] is True
    assert result["remote_size"] == {"count": 12, "bytes": 3456}
    assert calls[1][-2:] == ["--", draft["remote"]]


def test_schedule_preview_returns_timezone_aware_next_runs():
    from app.routes import api_config

    result = api_config.preview_schedule(
        api_config.SchedulePreviewRequest(
            expression="0 3 * * *", timezone="Europe/Berlin", count=3
        )
    )

    assert result["ok"] is True
    assert result["enabled"] is True
    assert result["timezone"] == "Europe/Berlin"
    assert len(result["next_runs"]) == 3
    assert all("+" in item["iso"] for item in result["next_runs"])
    assert [item["timestamp"] for item in result["next_runs"]] == sorted(
        item["timestamp"] for item in result["next_runs"]
    )


def test_schedule_preview_supports_manual_and_rejects_invalid_cron():
    from fastapi import HTTPException

    from app.routes import api_config

    manual = api_config.preview_schedule(
        api_config.SchedulePreviewRequest(expression="manual")
    )
    assert manual["enabled"] is False
    assert manual["next_runs"] == []

    try:
        api_config.preview_schedule(
            api_config.SchedulePreviewRequest(expression="not a cron")
        )
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("invalid cron must be rejected")
