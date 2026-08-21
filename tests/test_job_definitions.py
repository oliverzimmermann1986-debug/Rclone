from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.config_validation import ConfigValidationError, validate_config
from app.db import Database
from app.job_definitions import effective_job_definitions, legacy_job_definitions
from app.jobs import rclone_sync
from app.jobs.scheduler import find_due_jobs
from app.routes import api_jobs


def _config(tmp_path: Path) -> dict:
    return {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {
            "default_schedule": "0 4 * * *",
            "scheduler_retry_minutes": 30,
            "pairs": [
                {
                    "name": "Fotos",
                    "remote": "cloud:/photos",
                    "local": str(tmp_path / "photos"),
                    "direction": "pull",
                    "mode": "copy",
                    "schedule": "0 2 * * *",
                },
                {
                    "name": "Dokumente",
                    "remote": "cloud:/docs",
                    "local": str(tmp_path / "docs"),
                    "direction": "pull",
                    "mode": "copy",
                },
            ],
        },
        "notifications": {"webhooks": []},
    }


def test_legacy_migration_is_idempotent_and_removes_pair_schedules(tmp_path: Path):
    first, _ = validate_config(_config(tmp_path))
    second, _ = validate_config(first)

    assert first == second
    assert all("schedule" not in pair for pair in first["backup"]["pairs"])
    assert [job["schedule"] for job in first["backup"]["jobs"]] == [
        "0 2 * * *",
        "0 4 * * *",
    ]
    assert all(job["execution_mode"] == "sequential" for job in first["backup"]["jobs"])


def test_legacy_id_uses_same_direction_mode_defaults_as_validator(tmp_path: Path):
    config = _config(tmp_path)
    pair = config["backup"]["pairs"][0]
    pair.pop("mode")
    pair["enabled"] = "false"
    config["backup"]["scheduler_retry_minutes"] = "ungültig"

    derived = legacy_job_definitions(config["backup"])[0]
    normalized, _ = validate_config(config)

    assert derived["data_path_ids"] == [normalized["backup"]["pairs"][0]["id"]]
    assert normalized["backup"]["jobs"][0]["enabled"] is False
    assert normalized["backup"]["jobs"][0]["retry_minutes"] == 60


def test_explicit_empty_jobs_stays_empty(tmp_path: Path):
    config = _config(tmp_path)
    config["backup"]["jobs"] = []
    normalized, _ = validate_config(config)
    assert normalized["backup"]["jobs"] == []
    assert effective_job_definitions(normalized) == []


def test_job_references_and_uniqueness_are_validated(tmp_path: Path):
    config = _config(tmp_path)
    config["backup"]["jobs"] = [
        {
            "id": "a" * 32,
            "name": "Nachtlauf",
            "data_path_ids": ["f" * 32],
            "schedule": "manual",
        },
        {
            "id": "a" * 32,
            "name": "Nachtlauf",
            "data_path_ids": ["f" * 32],
            "schedule": "manual",
        },
    ]
    with pytest.raises(ConfigValidationError) as caught:
        validate_config(config)
    message = str(caught.value)
    assert "unbekannte IDs" in message
    assert "Doppelter Job-Name" in message
    assert ".id ist doppelt" in message


def test_multiple_jobs_may_share_paths_and_preserve_path_order(tmp_path: Path):
    migrated, _ = validate_config(_config(tmp_path))
    path_ids = [pair["id"] for pair in migrated["backup"]["pairs"]]
    migrated["backup"]["jobs"] = [
        {
            "id": "1" * 32,
            "name": "Alle seriell",
            "data_path_ids": list(reversed(path_ids)),
            "schedule": "manual",
            "execution_mode": "sequential",
            "max_parallel": 8,
        },
        {
            "id": "2" * 32,
            "name": "Fotos parallel",
            "data_path_ids": [path_ids[0]],
            "schedule": "manual",
            "execution_mode": "parallel",
            "max_parallel": 4,
        },
    ]
    normalized, _ = validate_config(migrated)
    assert normalized["backup"]["jobs"][0]["data_path_ids"] == list(reversed(path_ids))
    assert normalized["backup"]["jobs"][0]["max_parallel"] == 1
    assert normalized["backup"]["jobs"][1]["max_parallel"] == 4


def test_run_metadata_and_history_survive_definition_rename(tmp_path: Path):
    database = Database(tmp_path / "runs.db")
    first = database.job_start(
        "backup",
        definition_id="a" * 32,
        definition_name="Alt",
        config_revision="rev-1",
        scheduled_slot="slot-1",
    )
    database.job_finish(first, "ok", {"ok": True, "trigger": "scheduler"})
    second = database.job_start(
        "backup",
        definition_id="a" * 32,
        definition_name="Neu",
        config_revision="rev-2",
        scheduled_slot="slot-2",
    )
    database.job_finish(second, "error", {"ok": False, "trigger": "scheduler"})

    assert database.job_get(first)["definition_name"] == "Alt"
    assert database.job_get(second)["definition_name"] == "Neu"
    assert database.job_get(second)["config_revision"] == "rev-2"
    history = database.job_definition_history({"a" * 32: "Neu"})["a" * 32]
    assert history["last_result"]["id"] == second
    assert history["last_success"]["id"] == first


def test_scheduler_uses_definition_history_and_job_retry(tmp_path: Path):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]
    definition["retry_minutes"] = 5
    now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()

    class History:
        def job_definition_history(self, definitions):
            assert list(definitions) == [job["id"] for job in config["backup"]["jobs"]]
            return {
                definition_id: {
                    "last_success": None,
                    "last_result": (
                        {
                            "ok": False,
                            "status": "error",
                            "ended_at": now - 6 * 60,
                            "trigger": "scheduler",
                            "id": 7,
                        }
                        if definition_id == definition["id"]
                        else None
                    ),
                }
                for definition_id in definitions
            }

    due, status = find_due_jobs(config, History(), now=now)
    assert definition["name"] in due
    item = next(row for row in status if row["definition_id"] == definition["id"])
    assert item["reason"] == "retry_after_failure"
    assert item["data_path_ids"] == definition["data_path_ids"]


def test_run_job_executes_exact_captured_snapshot_revision(tmp_path: Path, monkeypatch):
    old = {
        "paths": {"logs_dir": str(tmp_path / "old-logs")},
        "backup": {
            "enabled": True,
            "max_parallel": 1,
            "pairs": [
                {
                    "name": "Fotos",
                    "enabled": True,
                    "remote": "old:/photos",
                    "local": str(tmp_path / "photos"),
                    "direction": "pull",
                    "mode": "copy",
                }
            ],
        },
    }
    changed = {
        **old,
        "backup": {
            **old["backup"],
            "pairs": [{**old["backup"]["pairs"][0], "remote": "new:/photos"}],
        },
    }

    class ChangedConfig:
        def snapshot_with_revision(self):
            return changed, "rev-new"

    seen: list[str] = []

    def sync_pair(pair, *_args):
        seen.append(str(pair["remote"]))
        return {"name": pair["name"], "ok": True}

    monkeypatch.setattr(rclone_sync, "get_config", lambda: ChangedConfig())
    monkeypatch.setattr(rclone_sync, "_sync_pair", sync_pair)
    monkeypatch.setattr(rclone_sync.runtime_state, "begin_run", lambda *_a, **_k: "run")
    monkeypatch.setattr(rclone_sync.runtime_state, "finish_run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        rclone_sync.runtime_state, "update_pair", lambda *_a, **_k: None
    )

    summary = rclone_sync.run_job(
        dry_run=True,
        config_snapshot=old,
        config_revision="rev-old",
    )

    assert seen == ["old:/photos"]
    assert summary["config_revision"] == "rev-old"


def test_definition_plan_uses_ordered_paths_and_consistent_errors(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    path_ids = [pair["id"] for pair in config["backup"]["pairs"]]
    definition = {
        "id": "a" * 32,
        "name": "Geordnet",
        "enabled": True,
        "data_path_ids": list(reversed(path_ids)),
        "schedule": "manual",
        "execution_mode": "sequential",
        "max_parallel": 1,
        "retry_minutes": 60,
    }
    config["backup"]["jobs"] = [definition]

    class Store:
        def snapshot_with_revision(self):
            return config, "rev-plan"

    captured = {}

    def build_plan(*, dry_run, pairs_filter, config_snapshot):
        captured.update(
            dry_run=dry_run,
            pairs_filter=pairs_filter,
            config_snapshot=config_snapshot,
        )
        return {"ok": True, "pairs": []}

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs.rclone_job, "build_job_plan", build_plan)

    result = api_jobs.plan_job_definition(definition["id"], dry_run=True)
    assert captured["pairs_filter"] == ["Dokumente", "Fotos"]
    assert captured["config_snapshot"] is config
    assert result["config_revision"] == "rev-plan"

    with pytest.raises(HTTPException) as missing:
        api_jobs.plan_job_definition("f" * 32)
    assert missing.value.status_code == 404

    definition["enabled"] = False
    with pytest.raises(HTTPException) as disabled:
        api_jobs.plan_job_definition(definition["id"])
    assert disabled.value.status_code == 409
