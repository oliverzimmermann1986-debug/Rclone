from types import SimpleNamespace

from app.routes import api_diagnostics, api_maintenance


class _Config:
    def snapshot(self):
        return {
            "backup": {"pairs": [], "enabled": True},
            "paths": {
                "data_dir": "/data",
                "logs_dir": "/logs",
                "temp_dir": "/temp",
            },
        }


class _DB:
    def __init__(self):
        self.integrity_calls = 0
        self.stats_calls = 0

    def integrity_check(self):
        self.integrity_calls += 1
        return {"ok": True}

    def stats(self):
        self.stats_calls += 1
        return {"jobs": 0}

    def job_list(self, **_kwargs):
        return []

    def job_statistics(self, **_kwargs):
        return {"total": 0, "by_status": {}, "by_kind": {}}

    def pair_last_history(self, identities):
        return {key: {} for key in identities}

    def runtime_get(self, _key, default):
        return default


def test_doctor_page_reads_share_one_expensive_operational_snapshot(monkeypatch):
    config = _Config()
    database = _DB()
    system_calls = 0
    service_calls: list[str] = []
    rclone_calls: list[tuple[str, ...]] = []
    api_diagnostics._OPERATIONAL_CACHE = None
    api_diagnostics._OVERVIEW_CACHE = None

    def fake_system(_path):
        nonlocal system_calls
        system_calls += 1
        return {"memory": {}, "data_disk": {}, "pids": {}}

    def fake_service(unit):
        service_calls.append(unit)
        return ("enabled", "active")

    def fake_run(command, **_kwargs):
        rclone_calls.append(tuple(command))
        return SimpleNamespace(
            returncode=0,
            stdout="rclone v1.70.0\n" if command[-1] == "version" else "cloud:\n",
            stderr="",
        )

    monkeypatch.setattr(api_diagnostics, "get_config", lambda: config)
    monkeypatch.setattr(api_diagnostics, "get_db", lambda: database)
    monkeypatch.setattr(api_maintenance, "get_config", lambda: config)
    monkeypatch.setattr(api_maintenance, "get_db", lambda: database)
    monkeypatch.setattr(api_diagnostics, "system_snapshot", fake_system)
    monkeypatch.setattr(api_diagnostics, "_systemctl_state", fake_service)
    monkeypatch.setattr(api_diagnostics, "validate_config", lambda snapshot: (snapshot, []))
    monkeypatch.setattr(api_diagnostics, "_writable_dir", lambda path: {"name": path, "level": "ok", "ok": True, "message": "ok"})
    monkeypatch.setattr(api_diagnostics, "effective_job_definitions", lambda _cfg: [])
    monkeypatch.setattr(api_diagnostics, "build_job_plan", lambda **_kwargs: {"warnings": []})
    monkeypatch.setattr(api_diagnostics.subprocess, "run", fake_run)

    api_diagnostics.doctor()
    api_diagnostics._build_overview()
    database_status = api_maintenance.database_status()

    assert database_status == {
        "ok": True,
        "stats": {"jobs": 0},
        "integrity": {"ok": True},
    }
    assert database.integrity_calls == 1
    assert database.stats_calls == 1
    assert system_calls == 1
    assert service_calls == [
        "rclone-sync.timer",
        "sync-scheduler.timer",
        "rclone-sync-web.service",
    ]
    assert rclone_calls.count(("rclone", "version")) == 1
