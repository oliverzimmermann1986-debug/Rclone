"""Verdrahtung: PBS-Wartungsdrossel, Lockdown, Bitrot/Boot-Drill im Scheduler,
Job-Scopes, Push-Events, Doctor-Checks und Router-Registrierung."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, config_store, db as db_module, lockdown, push_notifications
from app import scheduler_control
from app.config_store import Config
from app.db import Database, JobAlreadyRunningError
from app.jobs import job_lifecycle, rclone_sync, runtime_state, scheduler_cli
from app.routes import api_diagnostics, api_pbs, api_recovery

NEW_ROUTERS = (
    "api_lockdown",
    "api_object_lock",
    "api_vpn",
    "api_versions",
    "api_bitrot",
    "api_bandwidth",
    "api_conflicts",
    "api_preflight",
    "api_recovery_diff",
    "api_boot_drill",
    "api_crypt",
)


def _boom(*_args, **_kwargs):
    raise AssertionError("darf nicht aufgerufen werden")


class _Config:
    revision = "rev-1"

    def __init__(self, data):
        self.data = data

    def get(self, *keys, default=None):
        value = self.data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


class _Db:
    def __init__(self):
        self.started: list[tuple[str, dict]] = []
        self.finished: list[tuple[int, str, dict]] = []
        self.runtime: dict = {}

    def job_start(self, kind, **kwargs):
        self.started.append((kind, kwargs))
        return 7

    def job_finish(self, job_id, status, summary):
        self.finished.append((job_id, status, summary))
        return True

    # Zeitstempel der PBS-Drossel (scheduler_cli._THROTTLE_SINCE_KEY).
    def runtime_get(self, key, default=None):
        return self.runtime.get(key, default)

    def runtime_set(self, key, value):
        self.runtime[key] = value

    def runtime_delete(self, key):
        self.runtime.pop(key, None)


def _scheduler(monkeypatch, tmp_path, *, lock_free=True):
    env = SimpleNamespace(
        config=_Config(
            {
                "backup": {"enabled": True, "pairs": []},
                "paths": {"logs_dir": str(tmp_path)},
                "pbs": {"enabled": True, "repository": "u@pbs!t@host:store"},
            }
        ),
        db=_Db(),
        locks=[],
        reconciled=[],
    )

    @contextmanager
    def lock(name):
        env.locks.append(name)
        yield object() if lock_free else None

    def reconcile(_db, *, scope, kinds):
        env.reconciled.append((scope, kinds))
        return {"safe": True}

    patches = {
        "get_config": lambda: env.config,
        "get_db": lambda: env.db,
        "_configure_logging": lambda _path=None: None,
        "_reconcile_available_scopes": lambda _db: None,
        "check_overdue": lambda *_args: [],
        "is_locked_down": lambda _db: False,
        "scheduler_state": lambda _db: {"paused": False},
        "file_lock_or_none": lock,
        "reconcile_locked_scope": reconcile,
        "reset_cancel": lambda *_args: None,
        "find_due_pairs": lambda *_args: ([], []),
        "find_due_pbs_targets": lambda *_args: ([], []),
        "restore_test_due": lambda *_args: {"due": False},
        "bitrot_scrub_due": lambda *_args: {"due": False},
        "boot_drill_due": lambda *_args: {"due": False},
        "maintenance_active": lambda _settings: {"active": False, "tasks": []},
        "run_job": _boom,
        "run_pbs_backup": _boom,
        "run_restore_test": _boom,
        "run_bitrot_scrub": _boom,
        "run_boot_drill": _boom,
    }
    for name, value in patches.items():
        monkeypatch.setattr(scheduler_cli, name, value)
    monkeypatch.setattr(push_notifications, "dispatch_pending_pushes", lambda **_k: {})
    return env


def test_pbs_maintenance_defers_rclone_but_pbs_backup_still_runs(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    seen = []

    def maintenance(settings):
        seen.append(settings)
        return {"active": True, "tasks": [{"type": "garbage_collection"}]}

    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pairs",
        lambda *_args: (["docs"], [{"name": "docs", "due": True}]),
    )
    monkeypatch.setattr(scheduler_cli, "maintenance_active", maintenance)
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pbs_targets",
        lambda *_args: (
            ["vm"],
            [
                {
                    "name": "vm",
                    "run_name": "pbs:vm",
                    "history_key": "pbs:id:vm",
                    "scheduled_slot": "s",
                    "due": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        scheduler_cli, "run_pbs_backup", lambda *_a, **_k: {"ok": True, "pairs": []}
    )
    # Boot-Drill wartet nach einem PBS-Backup im selben Tick auf den nächsten.
    monkeypatch.setattr(scheduler_cli, "boot_drill_due", lambda *_a: {"due": True})

    assert scheduler_cli.main() == 0
    assert seen == [env.config.data["pbs"]]
    assert [kind for kind, _ in env.db.started] == ["pbs"]


def test_lockdown_stops_scheduler_before_any_job(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler_cli, "is_locked_down", lambda _db: True)
    monkeypatch.setattr(scheduler_cli, "scheduler_state", _boom)
    monkeypatch.setattr(scheduler_cli, "find_due_pairs", _boom)

    assert scheduler_cli.main() == 0
    assert env.db.started == []


def test_lockdown_blocks_manual_pbs_and_recovery_starts(monkeypatch):
    monkeypatch.setattr(api_pbs, "is_locked_down", lambda: True)
    monkeypatch.setattr(api_pbs, "get_config", _boom)
    released = []
    lease = SimpleNamespace(release=lambda: released.append(True))
    database = SimpleNamespace(
        job_terminal_recover_pending=lambda: {},
        job_start=_boom,
    )
    monkeypatch.setattr(api_recovery, "is_locked_down", lambda: True)
    monkeypatch.setattr(api_recovery, "try_file_lock", lambda _scope: lease)
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    monkeypatch.setattr(
        api_recovery, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    monkeypatch.setattr(rclone_sync, "reset_cancel", lambda: None)

    with pytest.raises(HTTPException) as pbs:
        api_pbs.pbs_run(api_pbs.PbsRunPayload())
    with pytest.raises(HTTPException) as recovery:
        api_recovery._start_recovery_job()

    assert pbs.value.status_code == recovery.value.status_code == 423
    assert released == [True]


def test_activate_lockdown_notifies_and_tolerates_notify_failure(tmp_path, monkeypatch):
    database = Database(tmp_path / "app.db")
    sent = []

    def notify(*args, **_kwargs):
        sent.append(args)
        raise RuntimeError("APNs nicht erreichbar")

    monkeypatch.setattr(
        lockdown,
        "get_config",
        lambda: SimpleNamespace(get=lambda *_k, default=None: []),
    )
    monkeypatch.setattr(rclone_sync, "cancel_job", lambda scope: {"ok": True})
    monkeypatch.setattr(
        runtime_state, "terminate_active_processes", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(scheduler_control, "pause_scheduler", lambda **_k: {"until": 1})
    monkeypatch.setattr(auth, "bump_session_version", lambda: 2)
    monkeypatch.setattr(lockdown, "send_push_notifications", notify)

    report = lockdown.activate_lockdown(actor="admin", reason="x" * 400, db=database)

    assert sent == [
        ("lockdown", "Notfall-Lockdown aktiviert", ("Grund: " + "x" * 300)[:300])
    ]
    assert report["notify"]["ok"] is False
    assert (report["notify"]["delivered"], report["notify"]["failed"]) == (0, 0)
    assert report["sessions_invalidated"] is True
    assert lockdown.is_locked_down(database)


def test_due_bitrot_scrub_runs_as_scheduler_job(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    runs = []

    def run(**kwargs):
        runs.append(kwargs)
        return {
            "kind": "bitrotscrub",
            "ok": True,
            "pairs": [],
            "history_keys": {"docs": "bitrotscrub:docs"},
        }

    monkeypatch.setattr(
        scheduler_cli,
        "bitrot_scrub_due",
        lambda *_a: {"due": True, "scheduled_slot": "slot-1"},
    )
    monkeypatch.setattr(scheduler_cli, "run_bitrot_scrub", run)

    assert scheduler_cli.main() == 0
    kind, kwargs = env.db.started[0]
    assert kind == "bitrotscrub"
    assert kwargs["attempts"] == [
        {
            "name": "bitrot-scrub",
            "history_key": "bitrotscrub:global",
            "scheduled_slot": "slot-1",
            "trigger": "scheduler",
        }
    ]
    assert kwargs["exclusive_scope"] is True
    assert kwargs["config_revision"] == "rev-1"
    assert runs == [
        {
            "trigger": "scheduler",
            "reset_cancel_state": False,
            "config_snapshot": env.config.data,
        }
    ]
    _job_id, status, summary = env.db.finished[0]
    assert status == "ok"
    assert summary["history_keys"] == {
        "docs": "bitrotscrub:docs",
        "bitrot-scrub": "bitrotscrub:global",
    }
    assert summary["scheduler_slots"] == {"bitrot-scrub": "slot-1"}
    assert env.reconciled == [("backup", job_lifecycle.BACKUP_KINDS)]


def test_bitrot_scrub_waits_for_next_tick_when_sync_was_due(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path, lock_free=False)
    monkeypatch.setattr(
        scheduler_cli,
        "find_due_pairs",
        lambda *_args: (["docs"], [{"name": "docs", "due": True}]),
    )
    monkeypatch.setattr(scheduler_cli, "bitrot_scrub_due", lambda *_a: {"due": True})

    assert scheduler_cli.main() == 0
    assert env.locks == ["backup"]  # nur der Sync-Block hat den Scope angefragt


def test_due_boot_drill_runs_in_pbs_scope(tmp_path, monkeypatch):
    env = _scheduler(monkeypatch, tmp_path)
    runs = []

    def run(**kwargs):
        runs.append(kwargs)
        return {
            "kind": "bootdrill",
            "ok": True,
            "pairs": [
                {"name": "boot-drill", "ok": True, "history_key": "bootdrill:global"}
            ],
        }

    monkeypatch.setattr(
        scheduler_cli,
        "boot_drill_due",
        lambda *_a: {"due": True, "scheduled_slot": "slot-b"},
    )
    monkeypatch.setattr(scheduler_cli, "run_boot_drill", run)

    assert scheduler_cli.main() == 0
    kind, kwargs = env.db.started[0]
    assert kind == "bootdrill"
    assert kwargs["attempts"] == [
        {
            "name": "boot-drill",
            "history_key": "bootdrill:global",
            "scheduled_slot": "slot-b",
            "trigger": "scheduler",
        }
    ]
    assert kwargs["exclusive_scope"] is True
    assert runs == [{"trigger": "scheduler", "config_snapshot": env.config.data}]
    _job_id, status, summary = env.db.finished[0]
    assert status == "ok"
    assert summary["scheduler_slots"] == {"boot-drill": "slot-b"}
    assert env.locks == ["pbs"]
    assert env.reconciled == [("pbs", ("pbs", "bootdrill"))]


@pytest.mark.parametrize(
    "case,expected_rc,started,finished_status",
    [
        ("busy", 0, False, None),
        ("unsafe", 1, False, None),
        ("already_done", 0, False, None),
        ("run_raises", 1, True, "error"),
        ("run_failed", 1, True, "error"),
        ("finish_lost", 1, True, None),
    ],
)
def test_global_drill_edge_cases(
    tmp_path, monkeypatch, case, expected_rc, started, finished_status
):
    env = _scheduler(monkeypatch, tmp_path, lock_free=case != "busy")
    if case == "unsafe":
        monkeypatch.setattr(
            scheduler_cli, "reconcile_locked_scope", lambda *_a, **_k: {"safe": False}
        )
    if case == "finish_lost":
        monkeypatch.setattr(scheduler_cli, "_finish_external_result", _boom)

    def run(_snapshot):
        if case == "run_raises":
            raise RuntimeError("kaputt")
        return {"ok": case != "run_failed", "pairs": []}

    rc = scheduler_cli._run_global_drill(
        env.db,
        env.config,
        tmp_path,
        label="Test-Drill",
        scope="backup",
        kinds=("backup",),
        kind="bitrotscrub",
        log_scope="bitrotscrub",
        run_name="drill",
        history_key="drill:global",
        due_check=lambda *_a: {"due": case != "already_done"},
        run=run,
    )

    assert rc == expected_rc
    assert bool(env.db.started) is started
    assert [status for _id, status, _s in env.db.finished] == (
        [finished_status] if finished_status else []
    )


def test_job_scopes_cover_bitrot_and_boot_drill(tmp_path):
    assert job_lifecycle.PBS_KINDS == ("pbs", "bootdrill")
    assert "bitrotscrub" in db_module._BACKUP_SCOPE_KINDS
    database = Database(tmp_path / "app.db")
    database.job_start("pbs", exclusive_scope=True)
    database.job_start("backup", exclusive_scope=True)
    with pytest.raises(JobAlreadyRunningError):
        database.job_start("bootdrill", exclusive_scope=True)
    with pytest.raises(JobAlreadyRunningError):
        database.job_start("bitrotscrub", exclusive_scope=True)


def test_new_incident_events_are_default_and_categorized():
    for event in ("bitrot_detected", "boot_drill_error", "lockdown"):
        assert event in push_notifications.DEFAULT_ERROR_EVENTS
        payload = json.loads(push_notifications._payload({"event": event}))
        assert payload["aps"]["category"] == "RCLONE_INCIDENT"


@pytest.mark.parametrize(
    "snapshot,tools,dev_zfs,expected",
    [
        ({"backup": {"pairs": [{"airgap_device": "/dev/sdb", "enabled": False}]}}, set(), False, []),
        ({"backup": {"pairs": [{"airgap_device": "/dev/sdb"}]}}, {"udisksctl"}, False, [("Airgap", "error")]),
        ({"backup": {"pairs": [{"airgap_device": "/dev/sdb"}]}}, {"udisksctl", "findmnt"}, False, [("Airgap", "ok")]),
        ({"backup": {"pairs": [{"zfs_snapshot": True}]}}, {"zfs"}, False, [("ZFS", "error")]),
        ({"backup": {"pairs": [{"zfs_snapshot": True}]}}, {"zfs"}, True, [("ZFS", "ok")]),
        ({"pbs": {"boot_drill": {"enabled": True}}}, set(), False, [("Boot-Drill", "warn"), ("Boot-Drill", "warn")]),
        ({"pbs": {"boot_drill": {"enabled": True}}}, {"qemu-system-x86_64"}, False, [("Boot-Drill", "ok"), ("Boot-Drill", "warn")]),
    ],
)  # fmt: skip
def test_doctor_checks_only_enabled_integrations(
    monkeypatch, snapshot, tools, dev_zfs, expected
):
    monkeypatch.setattr(
        api_diagnostics.shutil, "which", lambda tool: tool in tools or None
    )
    monkeypatch.setattr(
        api_diagnostics.os, "access", lambda path, _mode: dev_zfs and path == "/dev/zfs"
    )
    monkeypatch.setattr(api_diagnostics, "vpn_snapshot", _boom)

    checks = api_diagnostics._integration_checks(snapshot)

    assert [(check["name"], check["level"]) for check in checks] == expected
    if expected and expected[0] == ("ZFS", "error"):
        assert "DeviceAllow=/dev/zfs rw" in checks[0]["message"]


@pytest.mark.parametrize("reachable,level", [(True, "ok"), (False, "warn")])
def test_doctor_reports_vpn_probes(monkeypatch, reachable, level):
    snapshot = {"backup": {"vpn_probes": ["nas:445"]}}
    seen = []

    def vpn(cfg):
        seen.append(cfg.get("backup", "vpn_probes", default=[]))
        return {"probes": [{"target": "nas:445", "reachable": reachable}]}

    monkeypatch.setattr(api_diagnostics, "vpn_snapshot", vpn)

    checks = api_diagnostics._integration_checks(snapshot)

    assert [(check["name"], check["level"]) for check in checks] == [("VPN", level)]
    assert seen == [["nas:445"]]


def test_new_routers_are_registered_and_require_login(tmp_path, monkeypatch):
    from app import main, routes

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "test-secret-which-is-long-enough-123456789",
                    "allowed_hosts": ["testserver"],
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path),
                    "temp_dir": str(tmp_path),
                },
                "backup": {"enabled": True, "pairs": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "_config", Config(config_path))
    monkeypatch.setattr(db_module, "_db", Database(tmp_path / "app.db"))
    # FastAPI >= 0.139 haelt eingebundene Router als _IncludedRouter.
    registered = {
        id(getattr(route, "original_router", None)) for route in main.app.routes
    }
    client = TestClient(main.app)
    assert client.get("/api/gibt-es-nicht").status_code == 404

    for name in NEW_ROUTERS:
        router = getattr(routes, name).router
        assert id(router) in registered, name
        for route in router.routes:
            for method in route.methods:
                path = re.sub(r"\{[^}]+\}", "x", route.path)
                response = client.request(method, path)
                assert response.status_code in {401, 403}, (name, method, path)
