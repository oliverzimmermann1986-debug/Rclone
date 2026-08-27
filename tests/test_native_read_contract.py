"""Backend-verankerte Shared-Fixtures für die native iOS-Lese-API."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import (
    api_browse,
    api_config,
    api_diagnostics,
    api_jobs,
    api_maintenance,
    api_pbs,
    api_push,
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

    def job_search(self, **_kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        return [copy.deepcopy(self.job)], 1

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
                    "pair": {"transferred": "2 KiB"},
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
        "browse_local",
        "maintenance_audit",
        "maintenance_logs",
        "maintenance_database",
        "push_status",
        "config_snapshots",
        "filter_file",
    }
    assert set(contract["endpoints"]) == expected
    for endpoint in contract["endpoints"].values():
        assert endpoint["method"] == "GET"
        assert endpoint["path"].startswith("/api/")


def test_new_native_operations_fixtures_match_supported_read_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: _Config
):
    browse_root = tmp_path / "browse"
    browse_root.mkdir()
    monkeypatch.setattr(api_browse, "_browse_roots", lambda: [browse_root])
    _assert_shape(api_browse.browse_local(""), _body("browse_local"))

    class OperationsDB:
        def audit_list(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return copy.deepcopy(_body("maintenance_audit")["events"])

        def stats(self) -> dict[str, Any]:
            return copy.deepcopy(_body("maintenance_database")["stats"])

        def integrity_check(self) -> dict[str, Any]:
            return copy.deepcopy(_body("maintenance_database")["integrity"])

    monkeypatch.setattr(api_maintenance, "get_db", lambda: OperationsDB())
    monkeypatch.setattr(api_maintenance, "get_config", lambda: config)
    _assert_shape(
        api_maintenance.audit_events(limit=100, event_type=""),
        _body("maintenance_audit"),
    )
    _assert_shape(api_maintenance.database_status(), _body("maintenance_database"))

    class PushConfig:
        def get(self, *keys: str, default: Any = None) -> Any:
            if keys == ("notifications", "apns"):
                return {
                    "enabled": True,
                    "team_id": "ABCDEFGHIJ",
                    "key_id": "KLMNOPQRST",
                    "key_file": "/data/AuthKey.p8",
                    "topic": "de.example.rclone",
                    "events": ["sync_error"],
                    "device_lease_days": 7,
                }
            return default

    class PushDB:
        def push_devices(self, *, limit: int) -> list[dict[str, Any]]:
            assert limit == 128
            return [{"token": "ab" * 32}]

        def push_outbox_status(self) -> dict[str, Any]:
            return copy.deepcopy(_body("push_status")["outbox"])

    monkeypatch.setattr(api_push, "get_config", lambda: PushConfig())
    monkeypatch.setattr(api_push, "get_db", lambda: PushDB())
    _assert_shape(api_push.push_status(), _body("push_status"))

    log_root = tmp_path / "logs"
    log_root.mkdir()
    log_path = log_root / "backup.log"
    log_path.write_bytes(b"log")
    monkeypatch.setattr(api_maintenance, "logs_root", lambda: log_root)
    monkeypatch.setattr(api_maintenance, "iter_logs", lambda _root: [log_path])
    _assert_shape(
        api_maintenance.list_logs(limit=200, query=""), _body("maintenance_logs")
    )

    monkeypatch.setattr(
        api_maintenance,
        "_snapshot_entries",
        lambda: copy.deepcopy(_body("config_snapshots")["snapshots"]),
    )
    _assert_shape(api_maintenance.list_config_snapshots(), _body("config_snapshots"))

    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("- *.tmp\n", encoding="utf-8")
    monkeypatch.setattr(api_config, "_filter_path", lambda: filter_path)
    _assert_shape(api_config.get_filter_file(), _body("filter_file"))


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
                    "last_progress_at": 1_720_000_025,
                    "stall_timeout_sec": 14_400,
                    "max_runtime_sec": None,
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
    assert (
        api_storage.overview(include_remote=False)["pairs"][0]["last_transferred"]
        == "2 KiB"
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


def test_native_management_uses_canonical_routes_and_models_without_webview():
    ios_root = CONTRACT_PATH.parents[1] / "ios" / "RcloneMobile"
    api = (ios_root / "Core" / "APIClient.swift").read_text(encoding="utf-8")
    models = (ios_root / "Core" / "Models.swift").read_text(encoding="utf-8")
    views = (ios_root / "Views" / "ConfigurationViews.swift").read_text(
        encoding="utf-8"
    )
    production = "\n".join(
        path.read_text(encoding="utf-8") for path in ios_root.rglob("*.swift")
    )

    for route in (
        "/api/config",
        "/api/jobs/definitions",
        "/plan?dry_run=",
        "/run?dry_run=",
        "/api/jobs/backup/quick",
        "/api/jobs/backup/check/",
        "/api/jobs/backup/restore-test",
        "/api/browse/local?path=",
        "/api/browse/rclone?path=",
        "/api/maintenance/audit?limit=",
        "/api/maintenance/config/snapshots",
        "/api/config/filter-file",
        "/api/config/change-password",
    ):
        assert route in api
    for status in ("status == 409", "status == 428", "status == 403", "status == 422"):
        assert status in api
    for model in (
        "struct PairConfig: Codable",
        "struct JobDefinition: Codable",
        "struct ConfigSnapshot: Codable",
        "struct JobPlan: Decodable",
    ):
        assert model in models
    assert "values.decodeIfPresent(String.self, forKey: .executionMode)" in models
    assert "values.decodeIfPresent([String].self, forKey: .dataPathIDs)" in models
    for feature in (
        "Datenwege in Reihenfolge",
        "Nicht gespeicherte Änderungen",
        "Serverstand laden",
        "Mit Passwort erneut speichern",
        "Löschungen ausdrücklich freigeben",
    ):
        assert feature in views
    operations = (ios_root / "Views" / "OperationalViews.swift").read_text(
        encoding="utf-8"
    )
    for feature in (
        "Quick Sync",
        "Systemweiten Restore-Test",
        "Audit-Protokoll",
        "Konfigurations-Snapshots",
        "Support-Bundle",
        "Filter-Datei",
        "Passwort ändern",
    ):
        assert feature in operations or feature in views
    assert "Webhooks" not in operations
    assert "WebhookManagementView" not in operations
    assert "browseTarget = .remote" in views
    assert "PathBrowserSheet" in views
    assert "timeout: includeSizes ? 85 : nil" in api
    assert "WKWebView" not in production
    assert "UIWebView" not in production


def test_native_f14_revision_safety_history_and_run_all_contracts():
    ios_root = CONTRACT_PATH.parents[1] / "ios" / "RcloneMobile"
    api = (ios_root / "Core" / "APIClient.swift").read_text(encoding="utf-8")
    app_model = (ios_root / "Core" / "AppModel.swift").read_text(encoding="utf-8")
    configuration = (ios_root / "Views" / "ConfigurationViews.swift").read_text(
        encoding="utf-8"
    )
    backups = (ios_root / "Views" / "BackupsView.swift").read_text(encoding="utf-8")
    dashboard = (ios_root / "Views" / "DashboardView.swift").read_text(encoding="utf-8")

    assert 'post("/api/jobs/definitions/run-all?dry_run=\\(dryRun)")' in api
    assert "runAllJobDefinitions()" in dashboard
    assert "model.runBackup()" not in dashboard
    assert "getJobDefinitions()" not in app_model
    assert "jobDefinitions = newConfig.backup.jobs" in app_model
    assert configuration.count("if configurationDraft.isDirty && !discardDirty") == 2
    assert configuration.count("Ungespeicherte") >= 2
    assert "searchJobs(" in backups
    assert "downloadJobsCSV(" in backups
    assert "downloadJobLog(" in backups
    assert backups.count("model.withCurrentClient") >= 5
    assert "detailError" in backups and "logError" in backups
    assert ".task(id: query)" in backups
    assert backups.count("guard generation == requestGeneration else { return }") >= 3
    assert "pbsState: ContentLoadState" in app_model
    assert "PBS-Status nicht geladen" in (
        ios_root / "Views" / "SystemView.swift"
    ).read_text(encoding="utf-8")


def test_native_configuration_draft_is_shared_across_data_paths_and_jobs():
    ios_root = CONTRACT_PATH.parents[1] / "ios" / "RcloneMobile"
    root = (ios_root / "Views" / "RootTabView.swift").read_text(encoding="utf-8")
    configuration = (ios_root / "Views" / "ConfigurationViews.swift").read_text(
        encoding="utf-8"
    )
    draft_store = (ios_root / "Core" / "ConfigurationDraftStore.swift").read_text(
        encoding="utf-8"
    )

    assert "@StateObject private var configurationDraft" in root
    assert ".environmentObject(configurationDraft)" in root
    assert (
        configuration.count(
            "@EnvironmentObject private var configurationDraft: ConfigurationDraftStore"
        )
        == 2
    )
    assert "paths: configurationDraft.pairs" in configuration
    assert "pairs: configurationDraft.pairs" in configuration
    assert "definitions: configurationDraft.definitions" in configuration
    assert "func upsertPair(_ pair: PairConfig, at index: Int?)" in draft_store
    assert (
        "func upsertDefinition(_ definition: JobDefinition, at index: Int?)"
        in draft_store
    )


def test_native_pbs_configuration_is_revision_safe_and_feature_complete():
    ios_root = CONTRACT_PATH.parents[1] / "ios" / "RcloneMobile"
    models = (ios_root / "Core" / "Models.swift").read_text(encoding="utf-8")
    app_model = (ios_root / "Core" / "AppModel.swift").read_text(encoding="utf-8")
    system = (ios_root / "Views" / "SystemView.swift").read_text(encoding="utf-8")
    view = (ios_root / "Views" / "PBSConfigurationView.swift").read_text(
        encoding="utf-8"
    )

    assert "var pbsConfiguration: PBSConfiguration" in models
    assert "replacingPBSConfiguration" in models
    assert "struct PBSTargetConfiguration: Codable, Equatable, Identifiable" in models
    assert "saveCompleteConfig(" in app_model
    assert "savePBSConfiguration" in app_model
    assert "PBSConfigurationView()" in system
    for feature in (
        "PBS-Integration aktiv",
        "Aufbewahrung",
        "Target hinzufügen",
        "Serverpfade – einer pro Zeile",
        "Mountpoint verlangen",
        "Mit Passwort speichern",
        "if isDirty && !discardDirty",
    ):
        assert feature in view
