from __future__ import annotations

from datetime import datetime
import copy
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import db as db_module
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


def test_dry_run_never_advances_definition_schedule_state(tmp_path: Path, monkeypatch):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]
    scheduled_now = datetime(
        2026, 8, 23, 2, 5, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()
    monkeypatch.setattr(db_module.time, "time", lambda: scheduled_now)
    database = Database(tmp_path / "dry-definition.db")

    dry_job = database.job_start(
        "backup",
        definition_id=definition["id"],
        definition_name=definition["name"],
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "web",
                "dry_run": True,
            }
        ],
    )
    database.job_finish(
        dry_job,
        "ok",
        {"ok": True, "dry_run": True, "trigger": "web", "pairs": []},
    )

    display = database.job_definition_history({definition["id"]: definition["name"]})[
        definition["id"]
    ]
    state = database.job_definition_schedule_state(
        {definition["id"]: definition["name"]}
    )[definition["id"]]
    due, status = find_due_jobs(config, database, now=scheduled_now)

    assert display["last_result"]["id"] == dry_job
    assert display["last_success"] is None
    assert state == {"last_result": None, "last_success": None}
    assert definition["name"] in due
    assert (
        next(item for item in status if item["definition_id"] == definition["id"])[
            "due"
        ]
        is True
    )


def test_failed_history_cleanup_preserves_scheduler_retry(tmp_path: Path, monkeypatch):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]
    definition["retry_minutes"] = 5
    now = datetime(2026, 8, 23, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()
    clock = [now - 6 * 60]
    monkeypatch.setattr(db_module.time, "time", lambda: clock[0])
    database = Database(tmp_path / "failed-cleanup.db")
    failed = database.job_start(
        "backup",
        definition_id=definition["id"],
        definition_name=definition["name"],
        scheduled_slot="failed-slot",
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "scheduler",
                "scheduled_slot": "failed-slot",
            }
        ],
    )
    database.job_finish(
        failed,
        "error",
        {
            "ok": False,
            "trigger": "scheduler",
            "scheduled_slot": "failed-slot",
            "pairs": [],
        },
    )
    clock[0] = now

    before_due, before_status = find_due_jobs(config, database, now=now)
    assert database.jobs_delete_failed() == 1
    after_due, after_status = find_due_jobs(config, database, now=now)

    assert database.job_definition_history({definition["id"]: definition["name"]})[
        definition["id"]
    ] == {"last_result": None, "last_success": None}
    assert before_due == after_due
    before = next(
        item for item in before_status if item["definition_id"] == definition["id"]
    )
    after = next(
        item for item in after_status if item["definition_id"] == definition["id"]
    )
    assert before["reason"] == after["reason"] == "retry_after_failure"
    assert before["scheduled_slot"] == after["scheduled_slot"]


def test_history_retention_preserves_definition_catch_up_state(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]
    clock = [datetime(2026, 8, 20, 2, 5, tzinfo=ZoneInfo("Europe/Berlin")).timestamp()]
    monkeypatch.setattr(db_module.time, "time", lambda: clock[0])
    database = Database(tmp_path / "retention-state.db")
    successful = database.job_start(
        "backup",
        definition_id=definition["id"],
        definition_name=definition["name"],
        scheduled_slot="success-slot",
        attempts=[
            {
                "name": "Fotos",
                "history_key": "rclone:id:photos",
                "trigger": "scheduler",
                "scheduled_slot": "success-slot",
            }
        ],
    )
    clock[0] += 60
    database.job_finish(
        successful,
        "ok",
        {
            "ok": True,
            "trigger": "scheduler",
            "scheduled_slot": "success-slot",
            "pairs": [],
        },
    )
    clock[0] = datetime(
        2026, 8, 23, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()

    before_due, before_status = find_due_jobs(config, database, now=clock[0])
    assert database.jobs_prune(older_than_days=1, keep_latest=0) == 1
    after_due, after_status = find_due_jobs(config, database, now=clock[0])

    assert database.job_definition_history({definition["id"]: definition["name"]})[
        definition["id"]
    ] == {"last_result": None, "last_success": None}
    assert before_due == after_due
    before = next(
        item for item in before_status if item["definition_id"] == definition["id"]
    )
    after = next(
        item for item in after_status if item["definition_id"] == definition["id"]
    )
    assert before["last_run"] == after["last_run"]
    assert before["scheduled_slot"] == after["scheduled_slot"]


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


def test_backup_run_without_pair_filter_uses_canonical_definition_batch(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        api_jobs,
        "_queue_enabled_job_definitions",
        lambda *, dry_run: (
            calls.append(("definitions", dry_run)) or {"ok": True, "canonical": True}
        ),
    )
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    monkeypatch.setattr(
        api_jobs,
        "_queue_backup",
        lambda *, dry_run, pairs_filter: (
            calls.append(("pairs", (dry_run, pairs_filter)))
            or {"ok": True, "canonical": False}
        ),
    )

    assert api_jobs.run_backup(dry_run=True, pairs=None)["canonical"] is True
    assert api_jobs.run_all_job_definitions(dry_run=False)["canonical"] is True
    assert api_jobs.run_backup(dry_run=True, pairs="Fotos")["canonical"] is False
    assert calls == [
        ("definitions", True),
        ("definitions", False),
        ("pairs", (True, ["Fotos"])),
    ]


def test_definition_batch_creates_separate_ordered_runs_under_one_global_lock(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    path_ids = [pair["id"] for pair in config["backup"]["pairs"]]
    config["backup"]["jobs"] = [
        {
            "id": "1" * 32,
            "name": "Seriell",
            "enabled": True,
            "data_path_ids": list(reversed(path_ids)),
            "schedule": "manual",
            "execution_mode": "sequential",
            "max_parallel": 1,
            "retry_minutes": 60,
        },
        {
            "id": "2" * 32,
            "name": "Parallel",
            "enabled": True,
            "data_path_ids": [path_ids[0]],
            "schedule": "manual",
            "execution_mode": "parallel",
            "max_parallel": 4,
            "retry_minutes": 60,
        },
        {
            "id": "3" * 32,
            "name": "Aus",
            "enabled": False,
            "data_path_ids": [path_ids[1]],
            "schedule": "manual",
            "execution_mode": "sequential",
            "max_parallel": 1,
            "retry_minutes": 60,
        },
    ]

    class Store:
        def snapshot_with_revision(self):
            return copy.deepcopy(config), "rev-batch"

    class FakeDb:
        def __init__(self):
            self.rows: dict[int, dict] = {}
            self.starts: list[dict] = []
            self.finishes: list[tuple[int, str, dict]] = []
            self.audits: list[tuple[str, dict]] = []
            self.batch = None

        def job_start(self, kind, **kwargs):
            job_id = len(self.rows) + 1
            self.rows[job_id] = {"id": job_id, "kind": kind, "status": "running"}
            self.starts.append({"kind": kind, **kwargs})
            return job_id

        def job_get(self, job_id):
            return dict(self.rows[job_id])

        def job_set_log_file(self, job_id, log_file):
            self.rows[job_id]["log_file"] = log_file

        def job_finish(self, job_id, status, summary):
            self.rows[job_id]["status"] = status
            self.finishes.append((job_id, status, summary))
            return True

        def audit_add(self, event, **kwargs):
            self.audits.append((event, kwargs))

        def job_batch_create(
            self, *, specs, snapshot, config_revision, dry_run, first_job_id
        ):
            self.batch = {
                "id": "batch-1",
                "state": "running",
                "dry_run": dry_run,
                "snapshot": snapshot,
                "config_revision": config_revision,
                "cancel_requested": False,
                "items": [
                    {
                        "position": position,
                        "state": "running" if position == 0 else "queued",
                        "job_id": first_job_id if position == 0 else None,
                        "spec": spec,
                    }
                    for position, spec in enumerate(specs)
                ],
            }
            return "batch-1"

        def job_batch_get(self, _batch_id):
            return copy.deepcopy(self.batch)

        def job_batch_item_start(self, _batch_id, position, job_id):
            item = self.batch["items"][position]
            if item["state"] != "queued":
                return False
            item.update(state="running", job_id=job_id)
            return True

        def job_batch_item_finish(self, _batch_id, position, state, **_kwargs):
            self.batch["items"][position]["state"] = state
            return True

    class FakeScopeLock:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    database = FakeDb()
    scope_lock = FakeScopeLock()
    runs: list[dict] = []

    def fake_run_job(**kwargs):
        runs.append(kwargs)
        return {
            "ok": True,
            "pairs": [{"name": name, "ok": True} for name in kwargs["pairs_filter"]],
        }

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "try_file_lock", lambda _scope: scope_lock)
    monkeypatch.setattr(
        api_jobs, "reconcile_locked_scope", lambda *_args, **_kwargs: {"safe": True}
    )
    monkeypatch.setattr(api_jobs.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        api_jobs, "_setup_job_logger", lambda *_args: (tmp_path / "run.log", None)
    )
    monkeypatch.setattr(
        api_jobs, "_finish_runtime_for_job", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(api_jobs.rclone_job, "run_job", fake_run_job)
    monkeypatch.setattr(api_jobs.rclone_job, "reset_cancel", lambda: None)
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)

    result = api_jobs._queue_enabled_job_definitions(dry_run=True)

    assert result["definition_ids"] == ["1" * 32]
    assert result["definition_names"] == ["Seriell"]
    assert result["started_definitions"] == [
        {
            "definition_id": "1" * 32,
            "definition_name": "Seriell",
            "state": "started",
            "job_id": 1,
        }
    ]
    assert result["queued_definitions"] == [
        {
            "definition_id": "2" * 32,
            "definition_name": "Parallel",
            "state": "queued",
            "job_id": None,
        }
    ]
    assert result["failed_definitions"] == []
    assert result["config_revision"] == "rev-batch"
    assert result["batch_id"] == "batch-1"
    assert [item["definition_id"] for item in database.starts] == [
        "1" * 32,
        "2" * 32,
    ]
    assert all(item["exclusive_scope"] is True for item in database.starts)
    assert [run["pairs_filter"] for run in runs] == [
        ["Dokumente", "Fotos"],
        ["Fotos"],
    ]
    assert [(run["execution_mode"], run["max_parallel_override"]) for run in runs] == [
        ("sequential", 1),
        ("parallel", 4),
    ]
    assert all(run["config_revision"] == "rev-batch" for run in runs)
    assert [status for _job_id, status, _summary in database.finishes] == ["ok", "ok"]
    assert scope_lock.releases == 1
    assert api_jobs._locks["backup"].locked() is False


def test_definition_batch_continues_after_reservation_failure_and_audits_item(
    monkeypatch,
):
    definitions = [
        {"id": char * 32, "name": name, "execution_mode": "sequential"}
        for char, name in (("1", "Erster"), ("2", "Defekt"), ("3", "Dritter"))
    ]
    specs = [
        {
            "definition": definition,
            "pair_names": [definition["name"]],
            "history_keys": {definition["name"]: f"key-{index}"},
            "attempts": [],
        }
        for index, definition in enumerate(definitions, start=1)
    ]

    class FakeDb:
        def __init__(self):
            self.starts = []
            self.audits = []
            self.batch = {
                "id": "batch-recovery",
                "state": "running",
                "dry_run": False,
                "snapshot": {"backup": {}},
                "config_revision": "batch-revision",
                "cancel_requested": False,
                "items": [
                    {
                        "position": position,
                        "state": "running" if position == 0 else "queued",
                        "job_id": 101 if position == 0 else None,
                        "spec": spec,
                    }
                    for position, spec in enumerate(specs)
                ],
            }

        def job_start(self, _kind, **kwargs):
            self.starts.append(kwargs["definition_id"])
            if kwargs["definition_id"] == "2" * 32:
                raise RuntimeError("temporary reservation failure")
            return 303

        def audit_add(self, event, **kwargs):
            self.audits.append((event, kwargs))

        def job_batch_get(self, _batch_id):
            return copy.deepcopy(self.batch)

        def job_batch_item_start(self, _batch_id, position, job_id):
            self.batch["items"][position].update(state="running", job_id=job_id)
            return True

        def job_batch_item_finish(self, _batch_id, position, state, **_kwargs):
            self.batch["items"][position]["state"] = state
            return True

        def job_get(self, _job_id):
            return {"status": "ok"}

        def job_finish(self, *_args, **_kwargs):
            return True

    class FakeScopeLock:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    database = FakeDb()
    scope_lock = FakeScopeLock()
    executed = []
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    monkeypatch.setattr(
        api_jobs,
        "_run_backup_thread",
        lambda job_id, *_args, **_kwargs: executed.append(job_id),
    )
    assert api_jobs._locks["backup"].acquire(blocking=False)

    api_jobs._run_definition_batch_thread("batch-recovery", scope_lock)

    assert database.starts == ["2" * 32, "3" * 32]
    assert executed == [101, 303]
    assert len(database.audits) == 1
    event, audit = database.audits[0]
    assert event == "job_definition_batch_item_failed"
    assert audit["details"] == {
        "batch_job_id": 101,
        "definition_id": "2" * 32,
        "definition_name": "Defekt",
        "state": "failed",
        "error_code": "reservation_failed",
        "error": "temporary reservation failure",
    }
    assert scope_lock.releases == 1
    assert api_jobs._locks["backup"].locked() is False


def test_persisted_definition_batch_cancel_skips_every_followup(tmp_path, monkeypatch):
    database = Database(tmp_path / "cancel-batch.db")
    first_job_id = database.job_start(
        "backup", exclusive_scope=True, definition_id="1" * 32
    )
    specs = [
        {
            "definition": {"id": "1" * 32, "name": "Erster"},
            "pair_names": ["Fotos"],
            "history_keys": {"Fotos": "rclone:id:fotos"},
            "attempts": [],
        },
        {
            "definition": {"id": "2" * 32, "name": "Zweiter"},
            "pair_names": ["Dokumente"],
            "history_keys": {"Dokumente": "rclone:id:dokumente"},
            "attempts": [],
        },
    ]
    batch_id = database.job_batch_create(
        specs=specs,
        snapshot={"backup": {}},
        config_revision="cancel-revision",
        dry_run=False,
        first_job_id=first_job_id,
    )

    class FakeScopeLock:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    scope_lock = FakeScopeLock()
    executed = []

    def run_first(job_id, *_args, **_kwargs):
        executed.append(job_id)
        database.job_finish(job_id, "ok", {"ok": True, "pairs": []})
        database.job_batch_request_cancel()

    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "_run_backup_thread", run_first)
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    monkeypatch.setattr(api_jobs, "_audit_best_effort", lambda *_args, **_kwargs: None)
    assert api_jobs._locks["backup"].acquire(blocking=False)

    api_jobs._run_definition_batch_thread(batch_id, scope_lock)

    batch = database.job_batch_get(batch_id)
    assert executed == [first_job_id]
    assert [item["state"] for item in batch["items"]] == ["done", "cancelled"]
    assert batch["state"] == "cancelled"
    assert scope_lock.releases == 1
    assert api_jobs._locks["backup"].locked() is False


def test_definition_batch_thread_start_failure_marks_first_job_error(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    first = copy.deepcopy(config["backup"]["jobs"][0])
    second = copy.deepcopy(first)
    second["id"] = "b" * 32
    second["name"] = "Zweiter"
    config["backup"]["jobs"] = [first, second]

    class Store:
        def snapshot_with_revision(self):
            return copy.deepcopy(config), "thread-failure-revision"

    class FakeDb:
        def __init__(self):
            self.finished = []

        def job_start(self, _kind, **_kwargs):
            return 88

        def job_finish(self, job_id, status, summary):
            self.finished.append((job_id, status, summary))
            return True

        def audit_add(self, *_args, **_kwargs):
            return None

        def job_batch_create(self, **_kwargs):
            return "batch-thread-failure"

    class FakeScopeLock:
        def __init__(self):
            self.releases = 0

        def release(self):
            self.releases += 1

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread capacity exhausted")

    database = FakeDb()
    scope_lock = FakeScopeLock()
    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "try_file_lock", lambda _scope: scope_lock)
    monkeypatch.setattr(
        api_jobs, "reconcile_locked_scope", lambda *_args, **_kwargs: {"safe": True}
    )
    monkeypatch.setattr(api_jobs.rclone_job, "reset_cancel", lambda: None)
    monkeypatch.setattr(api_jobs.threading, "Thread", BrokenThread)

    with pytest.raises(HTTPException) as caught:
        api_jobs._queue_enabled_job_definitions(dry_run=False)

    assert caught.value.status_code == 500
    assert database.finished == [
        (
            88,
            "error",
            {
                "ok": False,
                "error": (
                    "Batch-Thread konnte nicht gestartet werden: "
                    "thread capacity exhausted"
                ),
                "error_code": "thread_start_failed",
                "stage": "setup",
            },
        )
    ]
    assert scope_lock.releases == 1
    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._locks["backup"].release()


def test_definition_batch_rejects_enabled_job_without_active_data_path(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    config["backup"]["pairs"][0]["enabled"] = False
    config["backup"]["jobs"] = [
        {
            "id": "a" * 32,
            "name": "Ohne aktiven Weg",
            "enabled": True,
            "data_path_ids": [config["backup"]["pairs"][0]["id"]],
            "schedule": "manual",
            "execution_mode": "sequential",
            "max_parallel": 1,
            "retry_minutes": 60,
        }
    ]

    class Store:
        def snapshot_with_revision(self):
            return config, "rev-invalid"

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    with pytest.raises(HTTPException) as caught:
        api_jobs._queue_enabled_job_definitions(dry_run=True)
    assert caught.value.status_code == 409
    assert "ohne aktive Datenwege" in str(caught.value.detail)
    assert api_jobs._locks["backup"].locked() is False


def test_definition_batch_process_lock_contention_releases_web_lock(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))

    class Store:
        def snapshot_with_revision(self):
            return config, "rev-contention"

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs, "try_file_lock", lambda _scope: None)

    with pytest.raises(HTTPException) as caught:
        api_jobs._queue_enabled_job_definitions(dry_run=True)
    assert caught.value.status_code == 409
    assert "Prozess-Lock" in str(caught.value.detail)
    assert api_jobs._locks["backup"].locked() is False


def test_failed_definition_job_can_retry_only_against_same_revision(
    tmp_path: Path, monkeypatch
):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]
    queued: list[dict] = []

    class Store:
        def snapshot_with_revision(self):
            return config, "rev-same"

    class Db:
        def job_get(self, job_id):
            assert job_id == 41
            return {
                "id": 41,
                "kind": "backup",
                "status": "error",
                "definition_id": definition["id"],
                "config_revision": "rev-same",
            }

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs, "get_db", lambda: Db())
    monkeypatch.setattr(
        api_jobs,
        "_queue_backup",
        lambda **kwargs: queued.append(kwargs) or {"ok": True, "job_id": 42},
    )
    monkeypatch.setattr(api_jobs, "_audit_best_effort", lambda *_args, **_kwargs: None)

    result = api_jobs.retry_job(41)

    assert result["job_id"] == 42
    assert result["retry_of_job_id"] == 41
    assert queued[0]["definition"]["id"] == definition["id"]
    assert queued[0]["config_revision"] == "rev-same"
    assert queued[0]["pairs_filter"] == ["Fotos"]


def test_retry_rejects_configuration_drift_before_queueing(tmp_path: Path, monkeypatch):
    config, _ = validate_config(_config(tmp_path))
    definition = config["backup"]["jobs"][0]

    class Store:
        def snapshot_with_revision(self):
            return config, "rev-new"

    class Db:
        def job_get(self, _job_id):
            return {
                "kind": "backup",
                "status": "error",
                "definition_id": definition["id"],
                "config_revision": "rev-old",
            }

    monkeypatch.setattr(api_jobs, "get_config", lambda: Store())
    monkeypatch.setattr(api_jobs, "get_db", lambda: Db())
    monkeypatch.setattr(
        api_jobs,
        "_queue_backup",
        lambda **_kwargs: pytest.fail("Bei Revision-Drift darf kein Job starten"),
    )

    with pytest.raises(HTTPException) as caught:
        api_jobs.retry_job(41)

    assert caught.value.status_code == 409
    assert "neu planen" in str(caught.value.detail)


@pytest.mark.parametrize("status", ["ok", "skipped"])
def test_retry_rejects_non_failed_job_status(status: str, monkeypatch):
    class Db:
        def job_get(self, _job_id):
            return {
                "kind": "backup",
                "status": status,
                "definition_id": "a" * 32,
                "config_revision": "rev-same",
            }

    monkeypatch.setattr(api_jobs, "get_db", lambda: Db())

    with pytest.raises(HTTPException) as caught:
        api_jobs.retry_job(41)

    assert caught.value.status_code == 409
    assert "fehlgeschlagene" in str(caught.value.detail)
