"""Backend-verankerte Shared-Fixtures für die native iOS-Lese-API."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import (
    api_config,
    api_diagnostics,
    api_jobs,
    api_pbs,
    api_storage,
)


CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "native_read_contract_v1.json"
)


def _contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _body(name: str) -> Any:
    return copy.deepcopy(_contract()["endpoints"][name]["body"])


def _assert_shape(actual: Any, example: Any, path: str = "body") -> None:
    """Prüft repräsentative Pflichtfelder und deren JSON-Typ rekursiv."""
    if example is None:
        return
    if isinstance(example, dict):
        assert isinstance(actual, dict), f"{path} muss ein Objekt sein"
        for key, value in example.items():
            assert key in actual, f"{path}.{key} fehlt"
            _assert_shape(actual[key], value, f"{path}.{key}")
        return
    if isinstance(example, list):
        assert isinstance(actual, list), f"{path} muss eine Liste sein"
        if example:
            assert actual, f"{path} darf für diese Fixture nicht leer sein"
            _assert_shape(actual[0], example[0], f"{path}[0]")
        return
    if isinstance(example, (int, float)) and not isinstance(example, bool):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), (
            f"{path}: {type(actual).__name__} statt Zahl"
        )
        return
    expected_type = bool if isinstance(example, bool) else type(example)
    assert isinstance(actual, expected_type), (
        f"{path}: {type(actual).__name__} statt {expected_type.__name__}"
    )


class _Config:
    def __init__(self, snapshot: dict[str, Any], revision: str = "a" * 64):
        self._snapshot = copy.deepcopy(snapshot)
        self._revision = revision

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._snapshot)

    def snapshot_with_revision(self) -> tuple[dict[str, Any], str]:
        return self.snapshot(), self._revision

    def get(self, *keys: str, default: Any = None) -> Any:
        value: Any = self._snapshot
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return copy.deepcopy(value)


class _DB:
    def __init__(self):
        self.job = copy.deepcopy(_body("jobs_list")[0])

    def job_list(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(self.job)]

    def job_count(self, **_kwargs: Any) -> int:
        return 1

    def job_get(self, _job_id: int) -> dict[str, Any]:
        return copy.deepcopy(_body("job_detail"))

    def job_running(self, kind: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.job) if kind == "backup" else None

    def job_statistics(self, **_kwargs: Any) -> dict[str, Any]:
        return {"total": 1, "by_status": {"ok": 1}, "by_kind": {"backup": 1}}

    def pair_last_history(self, identities: dict[str, str]) -> dict[str, Any]:
        return {
            key: {
                "last_result": {
                    "status": "ok",
                    "ended_at": 1_719_990_000,
                    "job_id": 41,
                    "pair": {"error": None},
                },
                "last_success": {
                    "ended_at": 1_719_990_000,
                    "pair": {"transferred": 2048},
                },
            }
            for key in identities
        }

    def pair_last_success(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ended_at": 1_719_990_000}

    def runtime_get(self, _key: str, default: Any) -> Any:
        return copy.deepcopy(default)

    def runtime_delete(self, _key: str) -> None:
        return None

    def integrity_check(self) -> dict[str, bool]:
        return {"ok": True}

    def stats(self) -> dict[str, int]:
        return {"jobs": 1}


@pytest.fixture
def config() -> _Config:
    body = _body("config")
    revision = body.pop("_revision")
    return _Config(body, revision)


def test_contract_manifest_is_versioned_and_covers_native_reads():
    contract = _contract()
    assert contract["contract_version"] == 1
    expected = {
        "diagnostics_overview",
        "storage_without_sizes",
        "storage_with_sizes",
        "config",
        "jobs_list",
        "jobs_search",
        "job_detail",
        "job_log",
        "jobs_current",
        "backup_progress",
        "pbs_status",
        "scheduler_state",
        "doctor",
        "job_definitions",
    }
    assert set(contract["endpoints"]) == expected
    for endpoint in contract["endpoints"].values():
        assert endpoint["method"] == "GET"
        assert endpoint["path"].startswith("/api/")


def test_config_and_job_definition_fixtures_match_route_outputs(
    monkeypatch: pytest.MonkeyPatch, config: _Config
):
    monkeypatch.setattr(api_config, "get_config", lambda: config)
    monkeypatch.setattr(api_jobs, "get_config", lambda: config)

    _assert_shape(api_config.get_config_endpoint(), _body("config"))
    definitions = api_jobs.list_job_definitions()
    assert definitions == _body("job_definitions")


def test_job_read_fixtures_match_route_outputs(
    monkeypatch: pytest.MonkeyPatch, config: _Config
):
    database = _DB()
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "get_config", lambda: config)

    _assert_shape(
        api_jobs.list_jobs(kind=None, status=None, q="", limit=50, offset=0),
        _body("jobs_list"),
    )
    database.job = copy.deepcopy(_body("jobs_search")["items"][0])
    _assert_shape(
        api_jobs.search_jobs(kind=None, status=None, q="", limit=50, offset=0),
        _body("jobs_search"),
    )
    _assert_shape(api_jobs.job_detail(41), _body("job_detail"))
    _assert_shape(api_jobs.job_log(41, tail=600), _body("job_log"))
    database.job = copy.deepcopy(_body("jobs_list")[0])
    _assert_shape(api_jobs.status_current(), _body("jobs_current"))


def test_progress_fixture_matches_running_route_output(monkeypatch: pytest.MonkeyPatch):
    database = _DB()
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(
        api_jobs.rclone_job,
        "get_runtime_state",
        lambda: {
            "status": "running",
            "started_at": 1_720_000_000,
            "pairs": {
                "Fotos": {
                    "status": "running",
                    "log_file": "/logs/42-fotos.log",
                    "error": None,
                }
            },
        },
    )
    monkeypatch.setattr(
        api_jobs.rclone_job,
        "read_log_tail",
        lambda _path: "Transferred: 2 KiB / 4 KiB, 50%, 1 KiB/s, ETA 2s",
    )
    monkeypatch.setattr(
        api_jobs,
        "_latest_stats",
        lambda _text: {
            "transferred": "2 KiB",
            "total": "4 KiB",
            "percent": 50.0,
            "speed": "1 KiB/s",
            "eta": "2s",
        },
    )
    monkeypatch.setattr(api_jobs.time, "time", lambda: 1_720_000_030)

    _assert_shape(api_jobs.backup_progress(), _body("backup_progress"))


def test_scheduler_and_pbs_fixtures_match_route_outputs(
    monkeypatch: pytest.MonkeyPatch, config: _Config
):
    database = _DB()
    pause = _body("scheduler_state")
    database.runtime_get = lambda _key, _default: {
        key: pause[key] for key in ("paused", "until", "reason", "actor", "updated_at")
    }
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "get_config", lambda: config)
    monkeypatch.setattr(api_jobs.time, "time", lambda: 1_720_000_000)
    _assert_shape(api_jobs.get_scheduler_state(), pause)

    monkeypatch.setattr(api_pbs, "get_db", lambda: database)
    monkeypatch.setattr(api_pbs, "get_config", lambda: config)
    monkeypatch.setattr(
        api_pbs.pbs_backup,
        "pbs_settings",
        lambda: {
            "enabled": True,
            "repository": "backup@pbs@server:datastore",
            "namespace": "rclone",
            "targets": [
                {
                    "name": "config",
                    "paths": ["/opt/rclone-sync/data"],
                    "schedule": "0 4 * * *",
                }
            ],
        },
    )
    monkeypatch.setattr(
        api_pbs.pbs_backup, "pbs_targets", lambda settings: settings["targets"]
    )
    monkeypatch.setattr(api_pbs.pbs_backup, "client_path", lambda: Path("pbs-client"))
    monkeypatch.setattr(
        api_pbs, "next_run_after", lambda *_args, **_kwargs: 1_720_007_200
    )
    _assert_shape(api_pbs.pbs_status(), _body("pbs_status"))


def test_storage_fixtures_match_route_and_cache_states(
    monkeypatch: pytest.MonkeyPatch, config: _Config
):
    database = _DB()
    monkeypatch.setattr(api_storage, "get_config", lambda: config)
    monkeypatch.setattr(api_storage, "get_db", lambda: database)
    monkeypatch.setattr(
        api_storage,
        "_disk_usage",
        lambda _path: _body("storage_without_sizes")["pairs"][0]["local_disk"],
    )
    _assert_shape(
        api_storage.overview(include_remote=False), _body("storage_without_sizes")
    )

    pair = config.get("backup", "pairs")[0]
    examples = _body("storage_with_sizes")["pairs"]
    clock = [1_720_000_000.0]
    monkeypatch.setattr(api_storage.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        api_storage,
        "_rclone_size",
        lambda path: {"path": path, "count": 12, "bytes": 2048},
    )
    with api_storage._size_cache_lock:
        api_storage._size_cache.clear()
    fresh = api_storage._cached_rclone_size(pair, "source", "/mnt/fotos")
    cached = api_storage._cached_rclone_size(pair, "source", "/mnt/fotos")
    monkeypatch.setattr(
        api_storage, "_rclone_size", lambda path: {"path": path, "error": "Timeout"}
    )
    stale = api_storage._cached_rclone_size(
        pair, "source", "/mnt/fotos", force_refresh=True
    )
    failed = api_storage._cached_rclone_size(
        {**pair, "id": "failed"}, "source", "/mnt/failed"
    )
    for actual, expected_pair in zip(
        (fresh, cached, stale, failed), examples, strict=True
    ):
        _assert_shape(actual, expected_pair["source_size"])
    assert [
        result["measurement_status"] for result in (fresh, cached, stale, failed)
    ] == ["fresh", "cached", "stale", "failed"]


def test_diagnostics_fixtures_match_generated_route_shapes(
    monkeypatch: pytest.MonkeyPatch, config: _Config
):
    database = _DB()
    database.job = copy.deepcopy(_body("diagnostics_overview")["jobs"]["last"])
    monkeypatch.setattr(api_diagnostics, "get_config", lambda: config)
    monkeypatch.setattr(api_diagnostics, "get_db", lambda: database)
    monkeypatch.setattr(
        api_diagnostics,
        "system_snapshot",
        lambda _path: _body("diagnostics_overview")["system"],
    )
    monkeypatch.setattr(
        api_diagnostics, "_systemctl_state", lambda _unit: ("enabled", "active")
    )
    monkeypatch.setattr(api_diagnostics.time, "time", lambda: 1_720_000_000)
    overview = api_diagnostics._build_overview()
    _assert_shape(overview, _body("diagnostics_overview"))

    monkeypatch.setattr(
        api_diagnostics, "validate_config", lambda snapshot: (snapshot, [])
    )
    monkeypatch.setattr(
        api_diagnostics,
        "_writable_dir",
        lambda _path: {"level": "ok", "message": "beschreibbar"},
    )
    monkeypatch.setattr(
        api_diagnostics.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="rclone v1.70.0\n" if command[-1] == "version" else "cloud:\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        api_diagnostics, "build_job_plan", lambda **_kwargs: {"warnings": []}
    )
    monkeypatch.setattr(
        api_diagnostics,
        "_rclone_version_check",
        lambda: {"name": "rclone version", "level": "ok", "message": "aktuell"},
    )
    doctor = api_diagnostics.doctor()
    _assert_shape(doctor, _body("doctor"))
