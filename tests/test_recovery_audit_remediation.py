"""Regression cases for the recovery findings from the October audit."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.db import Database
from app import recovery_snapshots as snapshots
from app.jobs import selective_restore
from app.recovery_points import list_points, point_target
from app.restore_evidence import evaluate_restore_evidence, pair_binding
from app.config_validation import (
    ConfigValidationError,
    _strict_path_ancestor,
    validate_config,
)


def setup_paths(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    pair = {
        "id": "docs",
        "name": "Docs",
        "local": str(source),
        "remote": str(target),
        "direction": "push",
        "mode": "copy",
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "server"),
            "recovery_dir": str(tmp_path / "staging"),
        },
        "backup": {"pairs": [pair]},
    }
    return config, pair, target


def test_selective_full_restore_rejects_mutated_same_size_snapshot(
    tmp_path, monkeypatch
):
    config, pair, target = setup_paths(tmp_path)
    (target / "file.txt").write_bytes(b"original")
    point = snapshots.capture_snapshot(config, pair, max_total_mb=1)
    captured = Path(snapshots.snapshot_target(config, pair, point["id"]))
    (captured / "file.txt").write_bytes(b"modified")
    database = Database(tmp_path / "state.db")
    job_id = database.job_start("recovery")

    def run(command, *, timeout):
        assert command[1] != "lsjson", (
            "Historical evidence must come from the recorded manifest"
        )
        if command[1] == "copy":
            shutil.copyfile(captured / "file.txt", Path(command[-1]) / "file.txt")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(selective_restore, "_run", run)
    monkeypatch.setattr(selective_restore, "notify", lambda *a, **kw: None)
    record = selective_restore.run_selective_restore(
        database,
        config,
        pair,
        ["file.txt"],
        max_total_mb=1,
        job_id=job_id,
        source_override=str(captured),
        recovery_point=point["id"],
    )
    assert record["status"] == "error"
    assert "Prüfsumme" in record["error"]
    assert not (tmp_path / "staging" / f"recovery-{job_id}").exists()


def test_staging_cleanup_requires_registered_finished_recovery(tmp_path):
    config, _, _ = setup_paths(tmp_path)
    database = Database(tmp_path / "state.db")
    root = selective_restore.staging_root(config)
    (root / "private").mkdir()
    (root / "recovery-99").mkdir()
    for identifier in ("private", "recovery-99"):
        with pytest.raises(ValueError):
            selective_restore.remove_staging(database, config, identifier)
        assert (root / identifier).exists()
    selective_restore._save_record(database, {"id": "recovery-99", "status": "running"})
    with pytest.raises(ValueError):
        selective_restore.remove_staging(database, config, "recovery-99")
    selective_restore.reconcile_staging(database, config)
    assert not (root / "recovery-99").exists()
    assert selective_restore.list_staging(database, config)[0]["status"] == "error"


def test_staging_budget_counts_all_retained_data(tmp_path):
    config, _, _ = setup_paths(tmp_path)
    config["backup"]["recovery_staging_max_mb"] = 1
    database = Database(tmp_path / "state.db")
    selective_restore._save_record(
        database, {"id": "recovery-1", "status": "ready", "bytes": 1024 * 1024}
    )
    with pytest.raises(ValueError, match="Gesamtlimit"):
        selective_restore.check_staging_budget(database, config, 1)


def test_snapshot_retention_prunes_old_matching_point_and_preserves_newest(tmp_path):
    config, pair, target = setup_paths(tmp_path)
    (target / "file").write_bytes(b"data")
    first = snapshots.capture_snapshot(config, pair, max_total_mb=1)
    path = snapshots.snapshot_root(config) / first["id"] / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["created_at"] = time.time() - 91 * 86400
    path.write_text(json.dumps(manifest), encoding="utf-8")
    second = snapshots.capture_snapshot(config, pair, max_total_mb=1)
    assert not path.parent.exists()
    assert (snapshots.snapshot_root(config) / second["id"]).is_dir()


def test_bisync_exposes_both_archive_sides_and_live_remote(tmp_path):
    config, pair, _ = setup_paths(tmp_path)
    pair.update(
        direction="bisync", backup_dir1="versions/{date}", backup_dir2="versions/{date}"
    )
    stamp = "2026-10-29"
    for endpoint in (pair["local"], pair["remote"]):
        (Path(endpoint) / "versions" / stamp).mkdir(parents=True)
    points = list_points(config, pair)
    ids = {point["id"] for point in points}
    assert {"current", "current-remote", "local:" + stamp, "remote:" + stamp}.issubset(
        ids
    )
    assert point_target(config, pair, "remote:" + stamp) == str(
        Path(pair["remote"]) / "versions" / stamp
    )


def test_truncated_listing_cannot_be_complete_restore_evidence():
    pair = {
        "id": "docs",
        "local": "/source",
        "remote": "cloud:docs",
        "direction": "push",
    }
    attempt = {
        "ok": True,
        "ended_at": 1000,
        "pair": {
            "evidence_binding": pair_binding(pair),
            "evidence_checked_at": 1000,
            "sample_status": "complete",
            "sample_size": 3,
            "verified": 3,
            "truncated": True,
        },
    }
    evidence = evaluate_restore_evidence(
        pair, {"last_result": attempt, "last_success": attempt}, now=1100
    )
    assert evidence["state"] == "partial"
    assert evidence["coverage_complete"] is False
    assert evidence["valid"] is False


@pytest.mark.parametrize(
    "ancestor,child,expected",
    [
        ("cloud:", "cloud:docs", True),
        ("cloud:docs", "cloud:docs/child", True),
        ("cloud:docs", "cloud:docs", False),
        ("cloud:docs", "cloud:docs2", False),
        ("one:docs", "two:docs/child", False),
    ],
)
def test_vault_nested_ancestor_is_component_aware(ancestor, child, expected):
    assert _strict_path_ancestor(ancestor, child) is expected


def test_nested_foreign_vault_target_is_rejected_in_config(tmp_path):
    config, pair, _ = setup_paths(tmp_path)
    pair["remote"] = "cloud:parent"
    other = {
        **pair,
        "id": "child",
        "name": "Child",
        "local": str(tmp_path / "other"),
        "remote": "cloud:parent/child",
    }
    config["backup"]["pairs"].append(other)
    with pytest.raises(ConfigValidationError, match="Vault-Ziel"):
        validate_config(config)


def test_capture_reconcile_leaves_unknown_directory_untouched(tmp_path):
    config, _, _ = setup_paths(tmp_path)
    root = snapshots.snapshot_root(config, create=True)
    unknown = root / ".capture-unknown"
    known = root / ".capture-owned"
    unknown.mkdir()
    known.mkdir()
    (known / ".capture-owner.json").write_text(
        json.dumps(
            {"schema": "sicherpfad-capture-draft-v1", "point_id": "full-" + "a" * 32}
        ),
        encoding="utf-8",
    )
    snapshots.reconcile_snapshot_captures(config)
    assert unknown.is_dir()
    assert not known.exists()
