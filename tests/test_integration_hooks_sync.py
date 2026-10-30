"""Verdrahtung von Airgap, ZFS-Snapshot, Versions-Lifecycle, Ausschlüssen und Lockdown."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import airgap, versions_lifecycle
from app.auth import require_auth
from app.jobs import rclone_sync
from app.routes import api_jobs
from app.security import require_csrf

PUSH = {"name": "Fotos", "remote": "cloud:/Fotos", "direction": "push", "mode": "copy"}


def _command(pair: dict, exclude_paths: list[str] | None) -> list[str]:
    cmd, *_ = rclone_sync._build_pair_command(
        pair, [], True, config_snapshot={"backup": {}}, exclude_paths=exclude_paths
    )
    return cmd


def test_excludes_follow_protection_and_precede_includes():
    pair = {**PUSH, "local": "/data/Fotos", "include": ["keep/**"]}
    cmd = _command(pair, ["tmp/a[1].txt"])
    rules = [cmd[i + 1] for i, token in enumerate(cmd) if token == "--filter"]
    assert rules == [
        "- /Sicherpfad/**",
        "- /.sicherpfad-snapshots/**",
        "- /.rclone-versions/**",
        "- /tmp/a\\[1\\].txt",
        "+ keep/**",
        "- **",
    ]


def test_bisync_rejects_excludes():
    pair = {**PUSH, "local": "/data/Fotos", "direction": "bisync", "mode": "bisync"}
    with pytest.raises(ValueError, match="bisync"):
        _command(pair, ["tmp"])


@pytest.fixture
def queued(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos", "Musik"})
    monkeypatch.setattr(
        api_jobs, "_queue_backup", lambda **kw: calls.append(kw) or {"ok": True}
    )
    monkeypatch.setattr(
        api_jobs, "_queue_enabled_job_definitions", lambda **kw: {"ok": True}
    )
    return calls


@pytest.mark.parametrize(
    "pairs,excludes,detail",
    [
        ("Fotos", {"Musik": ["a"]}, "nicht ausgewählte Pairs: Musik"),
        ("Fotos", {"Gibtsnicht": ["a"]}, "nicht ausgewählte Pairs: Gibtsnicht"),
        ("Fotos", {"Fotos": ["/abs"]}, "relativ"),
        ("Fotos", {"Fotos": ["a/../b"]}, "..-Segment"),
        (None, {"Fotos": ["a"]}, "nur mit Pair-Auswahl"),
    ],
)
def test_run_backup_rejects_invalid_excludes(queued, pairs, excludes, detail):
    payload = api_jobs.BackupRunPayload(exclude_paths=excludes)
    with pytest.raises(HTTPException) as caught:
        api_jobs.run_backup(dry_run=False, pairs=pairs, payload=payload, user="admin")
    assert caught.value.status_code == 400
    assert detail in caught.value.detail
    assert queued == []


def test_run_backup_body_reaches_queue_and_stays_optional(queued):
    app = FastAPI()
    app.include_router(api_jobs.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app)

    body = {"exclude_paths": {"Fotos": ["Urlaub/IMG 1.jpg"]}}
    assert client.post("/api/jobs/backup/run?pairs=Fotos", json=body).status_code == 200
    assert client.post("/api/jobs/backup/run?pairs=Fotos").status_code == 200
    assert queued == [
        {
            "dry_run": False,
            "pairs_filter": ["Fotos"],
            "exclude_paths": {"Fotos": ["Urlaub/IMG 1.jpg"]},
            "actor": "admin",
        },
        {"dry_run": False, "pairs_filter": ["Fotos"], "actor": "admin"},
    ]


def test_queue_backup_forwards_excludes_and_audits_only_counts(monkeypatch, tmp_path):
    audits: list[dict] = []
    threads: list[tuple] = []
    snapshot = {"backup": {"pairs": [{**PUSH, "local": "/data/Fotos"}]}}
    monkeypatch.setattr(api_jobs, "is_locked_down", lambda: False)
    monkeypatch.setattr(api_jobs, "_reserve_backup_job", lambda *_a, **_k: (7, None))
    monkeypatch.setattr(
        api_jobs, "_audit_best_effort", lambda event, **kw: audits.append(kw["details"])
    )
    monkeypatch.setattr(
        api_jobs, "_start_thread", lambda _fn, **kw: threads.append(kw["args"])
    )

    try:
        api_jobs._queue_backup(
            dry_run=False,
            pairs_filter=["Fotos"],
            config_snapshot=snapshot,
            exclude_paths={"Fotos": ["a.txt", "b.txt"]},
        )
    finally:
        api_jobs._locks["backup"].release()  # sonst gibt der Worker-Thread ihn frei

    assert audits[0]["excluded_counts"] == {"Fotos": 2}
    assert "a.txt" not in repr(audits)
    assert threads[0][-1]["exclude_paths"] == {"Fotos": ["a.txt", "b.txt"]}


@pytest.mark.parametrize(
    "call",
    [
        lambda: api_jobs._queue_backup(dry_run=False, pairs_filter=["Fotos"]),
        lambda: api_jobs._queue_enabled_job_definitions(dry_run=False),
        lambda: api_jobs.start_restore_test(pairs=None, user="admin"),
        lambda: api_jobs.run_quick_sync(
            api_jobs.QuickSyncPayload(remote="cloud:/x", local="/data/x"), user="admin"
        ),
    ],
)
def test_lockdown_blocks_job_starts(monkeypatch, call):
    monkeypatch.setattr(api_jobs, "is_locked_down", lambda: True)
    monkeypatch.setattr(
        api_jobs, "get_config", lambda: pytest.fail("Config darf nicht gelesen werden")
    )
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == 423
    assert not api_jobs._locks["backup"].locked()


def test_bitrotscrub_is_a_backup_kind():
    from app.jobs.job_lifecycle import BACKUP_KINDS

    assert "bitrotscrub" in BACKUP_KINDS
    assert "bitrotscrub" in api_jobs._ALLOWED_JOB_KINDS


def test_run_job_hands_excludes_to_the_matching_pair(monkeypatch, tmp_path):
    seen: dict[str, object] = {}
    monkeypatch.setattr(rclone_sync, "get_config", lambda: object())
    monkeypatch.setattr(
        rclone_sync,
        "_sync_pair",
        lambda pair, *args: (
            seen.update({pair["name"]: args[-1]}) or {"name": pair["name"], "ok": True}
        ),
    )
    monkeypatch.setattr(rclone_sync.runtime_state, "begin_run", lambda *_a, **_k: "r")
    monkeypatch.setattr(rclone_sync.runtime_state, "finish_run", lambda *_a, **_k: None)
    monkeypatch.setattr(
        rclone_sync.runtime_state, "update_pair", lambda *_a, **_k: None
    )
    pairs = [
        {**PUSH, "local": str(tmp_path / "f")},
        {**PUSH, "name": "Musik", "local": str(tmp_path / "m")},
    ]
    rclone_sync.run_job(
        dry_run=True,
        config_snapshot={
            "paths": {"logs_dir": str(tmp_path / "logs")},
            "backup": {"pairs": pairs},
        },
        exclude_paths={"Fotos": ["a.txt"]},
    )
    assert seen == {"Fotos": ["a.txt"], "Musik": None}


@pytest.fixture
def sync(monkeypatch, tmp_path):
    """_sync_pair mit echtem Befehlsbau; rclone, Prechecks und Guards gemockt."""
    events: list[object] = []
    state = {"rc": 0, "anomaly_sources": []}

    def fake_run(cmd, log_file, **_kwargs):
        events.append(("rclone", cmd))
        Path(log_file).write_text("", encoding="utf-8")
        return state["rc"]

    def anomaly(_db, *, source, **_kwargs):
        state["anomaly_sources"].append(source)
        return {}

    monkeypatch.setattr(
        rclone_sync,
        "_precheck_pair",
        lambda _p: events.append("precheck") or (True, "ok"),
    )
    monkeypatch.setattr(rclone_sync, "_prepare_local_destinations", lambda _p: None)
    monkeypatch.setattr(rclone_sync, "_capture_local_endpoint_guards", lambda _p: {})
    monkeypatch.setattr(rclone_sync, "_set_active_pair_log", lambda *_a: None)
    monkeypatch.setattr(rclone_sync, "is_cancelled", lambda *_a: False)
    monkeypatch.setattr(rclone_sync, "_run_rclone_command", fake_run)
    monkeypatch.setattr(rclone_sync, "get_db", lambda: None)
    monkeypatch.setattr(
        rclone_sync.runtime_state, "update_pair", lambda *_a, **_k: None
    )
    monkeypatch.setattr(rclone_sync.protection, "preflight_anomaly_guard", anomaly)
    monkeypatch.setattr(
        rclone_sync.protection, "record_anomaly_baseline", lambda *_a, **_k: None
    )

    def run(pair: dict, *, dry_run: bool = False, exclude_paths=None) -> dict:
        return rclone_sync._sync_pair(
            pair, [], tmp_path, dry_run, 60, None, "run", {"backup": {}}, exclude_paths
        )

    run.events = events
    run.state = state
    return run


@pytest.fixture
def mounted(monkeypatch, tmp_path, sync):
    """Echte airgap_session; nur udisksctl-Aufrufe (mount/eject) gemockt."""
    mountpoint = tmp_path / "media"
    (mountpoint / "Fotos").mkdir(parents=True)
    monkeypatch.setattr(
        airgap,
        "mount_device",
        lambda _device, **_k: sync.events.append("mount") or str(mountpoint),
    )
    monkeypatch.setattr(
        airgap,
        "eject_device",
        lambda _device, **_k: (
            sync.events.append("eject")
            or {"unmounted": True, "powered_off": True, "error": None}
        ),
    )
    return str(mountpoint / "Fotos")


@pytest.mark.parametrize("rc", [0, 1])
def test_airgap_wraps_whole_pair_run_and_always_ejects(sync, mounted, rc):
    sync.state["rc"] = rc
    pair = {**PUSH, "local": mounted, "airgap_device": "/dev/disk/by-uuid/AB"}

    summary = sync(pair)

    assert summary["ok"] is (rc == 0)
    assert [e if isinstance(e, str) else e[0] for e in sync.events] == [
        "mount",
        "precheck",
        "rclone",
        "eject",
    ]
    assert summary["airgap"]["mountpoint"].endswith("media")
    assert summary["airgap"]["eject"]["powered_off"] is True


def test_airgap_setup_error_is_reported_without_traceback(
    sync, mounted, tmp_path, caplog
):
    pair = {**PUSH, "local": str(tmp_path / "elsewhere"), "airgap_device": "/dev/sdz1"}

    with caplog.at_level(logging.ERROR):
        summary = sync(pair)

    assert summary["error"] == "Lokaler Pfad liegt nicht auf dem Airgap-Gerät"
    assert summary["skipped"] is True and summary["ok"] is False
    assert sync.events == ["mount", "eject"]
    assert not any(record.exc_info for record in caplog.records)


@pytest.fixture
def zfs(monkeypatch):
    fake = SimpleNamespace(
        calls=[], cleanup_error=False, snap="/tank/.zfs/snapshot/sicherpfad-1/Fotos"
    )

    @contextlib.contextmanager
    def frozen(pair):
        fake.calls.append("create")
        result = {"active": True, "source": fake.snap, "snapshot": "s1"}
        try:
            yield result
        finally:
            fake.calls.append("destroy")
            if fake.cleanup_error:
                result["cleanup_error"] = "busy"

    monkeypatch.setattr(rclone_sync, "frozen_source", frozen)
    return fake


@pytest.mark.parametrize("rc", [0, 1])
def test_zfs_snapshot_is_rclone_source_and_destroyed(sync, zfs, tmp_path, rc):
    sync.state["rc"] = rc
    local = str(tmp_path / "Fotos")
    summary = sync({**PUSH, "local": local, "zfs_snapshot": True})

    cmd = sync.events[-1][1]
    assert cmd[-2:] == [zfs.snap, "cloud:/Fotos"]
    assert sync.state["anomaly_sources"] == [local]
    assert zfs.calls == ["create", "destroy"]
    assert summary["zfs_snapshot"]["snapshot"] == "s1"
    assert summary["local"] == local
    assert "warning" not in summary


def test_zfs_cleanup_error_becomes_warning(sync, zfs, tmp_path):
    zfs.cleanup_error = True
    summary = sync({**PUSH, "local": str(tmp_path / "Fotos"), "zfs_snapshot": True})
    assert summary["ok"] is True
    assert summary["zfs_snapshot"]["cleanup_error"] == "busy"
    assert summary["warning"].endswith("ZFS-Snapshot konnte nicht entfernt werden")


def test_zfs_dry_run_takes_no_snapshot(sync, zfs, tmp_path):
    local = str(tmp_path / "Fotos")
    summary = sync(
        {**PUSH, "local": local, "zfs_snapshot": True},
        dry_run=True,
        exclude_paths=["x.tmp"],
    )
    assert zfs.calls == []
    assert sync.events[-1][1][-2] == local
    assert "- /x.tmp" in sync.events[-1][1]
    assert summary["excluded_paths"] == ["x.tmp"]
    assert "zfs_snapshot" not in summary


@pytest.mark.parametrize(
    "dry_run,rc,result,expected_warning",
    [
        (False, 0, {"dry_run": False, "roots": [], "warnings": []}, None),
        (
            False,
            0,
            {"dry_run": False, "roots": [{"failed": [{"name": "x"}]}], "warnings": []},
            "Versionsordner nur teilweise ausgedünnt",
        ),
        (False, 0, RuntimeError("kaputt"), "Versionsordner nicht ausgedünnt"),
        (False, 0, {"skipped": True, "reason": "disabled"}, None),
        (True, 0, None, None),
        (False, 1, None, None),
    ],
)
def test_versions_lifecycle_only_after_successful_real_run(
    sync, monkeypatch, tmp_path, dry_run, rc, result, expected_warning
):
    calls: list[bool] = []

    def lifecycle(cfg, pair, *, dry_run):
        calls.append(dry_run)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(versions_lifecycle, "apply_lifecycle", lifecycle)
    sync.state["rc"] = rc
    summary = sync({**PUSH, "local": str(tmp_path / "Fotos")}, dry_run=dry_run)

    assert calls == ([False] if result is not None else [])
    assert summary.get("warning", "").endswith(expected_warning or "")
    assert ("warning" in summary) is (expected_warning is not None)
    stored = result is not None and not isinstance(result, Exception)
    assert ("versions_lifecycle" in summary) is (stored and "skipped" not in result)
