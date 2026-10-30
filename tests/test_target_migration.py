import copy
import hashlib
import json
import secrets
import subprocess
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import target_migration as migration
from app.auth import account_generation, require_auth
from app.config_store import Config, ConfigConflictError
from app.db import Database
from app.jobs.recovery_lifecycle import RecoveryCancelled
from app.routes import api_migrations


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = {
        "web": {
            "username": "admin",
            "secret_key": secrets.token_urlsafe(48),
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path / "server"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
            "device_vault_dir": str(tmp_path / "server/device-vault"),
        },
        "backup": {
            "pairs": [
                {
                    "id": "d" * 32,
                    "name": "Documents",
                    "direction": "push",
                    "mode": "copy",
                    "local": str(tmp_path / "local"),
                    "remote": "old:documents",
                }
            ]
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    store, database = Config(path), Database(tmp_path / "jobs.db")
    files = {
        "old:documents": {"report.txt": b"old", ".versions/old/report.txt": b"bak"},
        "new:documents": {},
    }
    commands = []

    def inventory(endpoint, **_kw):
        result = {
            p: {"directory": False, "size": len(content), "modified": "stamp"}
            for p, content in files[endpoint].items()
        }
        if files[endpoint]:
            result["empty"] = {"directory": True, "size": 0, "modified": "stamp"}
        return result

    def hashes(endpoint, _inventory):
        return {
            p: hashlib.sha256(content).hexdigest()
            for p, content in files[endpoint].items()
        }

    def run(command, **_kw):
        commands.append(command)
        assert command[:2] == ["rclone", "copy"]
        files[command[-1]] = copy.deepcopy(files[command[-2]])
        return subprocess.CompletedProcess(command, 0, "", "")

    lease = SimpleNamespace(held=False, releases=0)

    def release():
        lease.held = False
        lease.releases += 1

    def acquire(scope):
        assert scope == "backup"
        if lease.held:
            return None
        lease.held = True
        return lease

    lease.release = release
    monkeypatch.setattr(migration, "try_file_lock", acquire)
    monkeypatch.setattr(
        migration, "reconcile_locked_scope", lambda *_a, **_k: {"safe": True}
    )
    monkeypatch.setattr(migration.rclone_sync, "reset_cancel", lambda: None)
    monkeypatch.setattr(migration.rclone_sync, "is_cancelled", lambda: False)
    monkeypatch.setattr(migration, "is_locked_down", lambda *_a: False)
    monkeypatch.setattr(migration, "check_cancelled", lambda: None)
    monkeypatch.setattr(migration, "_inventory", inventory)
    monkeypatch.setattr(migration, "_hashes", hashes)
    monkeypatch.setattr(migration, "run_command", run)
    monkeypatch.setattr(migration, "_local_security", lambda *_a, **_k: None)
    return SimpleNamespace(
        store=store,
        db=database,
        files=files,
        commands=commands,
        lease=lease,
        root=tmp_path,
    )


def preview(env):
    return migration.preview(
        env.store,
        env.db,
        identity="d" * 32,
        target="new:documents",
        expected_revision=env.store.revision,
    )


def test_preview_copies_nothing_and_success_needs_separate_verified_cutover(setup):
    env = setup
    plan = preview(env)
    assert plan["status"] == "preview" and not env.commands
    assert plan["files"] == 2 and plan["total_bytes"] == 6
    completed = migration.start(env.store, env.db, plan["id"], background=False)
    assert completed["status"] == "verified"
    assert env.store.get("backup", "pairs")[0]["remote"] == "old:documents"
    assert env.db.job_get(completed["job_id"])["status"] == "ok"
    assert ".versions/old/report.txt" in env.files["new:documents"]
    assert (
        "--immutable" in env.commands[0]
        and "--create-empty-src-dirs" in env.commands[0]
    )
    assert not any("delete" in item or item == "sync" for item in env.commands[0])
    switched = migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    assert switched["status"] == "switched"
    assert env.store.get("backup", "pairs")[0]["remote"] == "new:documents"
    assert env.files["old:documents"]["report.txt"] == b"old"
    assert not env.lease.held


def test_stale_revision_and_invalid_id_release_lock_without_copy(setup):
    env = setup
    plan = preview(env)
    env.store.update(lambda cfg: cfg.update(note="new revision"))
    with pytest.raises(ConfigConflictError):
        migration.start(env.store, env.db, plan["id"], background=False)
    assert not env.lease.held and not env.commands
    with pytest.raises(migration.MigrationError, match="nicht gefunden"):
        migration.start(env.store, env.db, "missing", background=False)
    assert not env.lease.held


def test_bad_destination_readback_prevents_cutover_and_preserves_original(
    setup, monkeypatch
):
    env = setup
    plan = preview(env)
    original = migration.run_command

    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        env.files["new:documents"]["report.txt"] = b"bad"
        return result

    monkeypatch.setattr(migration, "run_command", corrupt)
    failed = migration.start(env.store, env.db, plan["id"], background=False)
    assert failed["status"] == "failed" and "SHA-256" in failed["error"]
    assert env.db.job_get(failed["job_id"])["status"] == "error"
    with pytest.raises(migration.MigrationError, match="erfolgreicher"):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    assert env.store.get("backup", "pairs")[0]["remote"] == "old:documents"


def test_cancelled_run_finishes_job_and_releases_lock(setup, monkeypatch):
    env = setup
    plan = preview(env)

    def cancel(*_a):
        raise RecoveryCancelled("cancelled")

    monkeypatch.setattr(migration, "_hashes", cancel)
    result = migration.start(env.store, env.db, plan["id"], background=False)
    assert result["status"] == "cancelled"
    assert env.db.job_get(result["job_id"])["status"] == "cancelled"
    assert not env.lease.held and not env.commands


@pytest.mark.parametrize(
    "target",
    [
        "old:documents/nested",
        "old:/documents/nested",
        "old:documents",
        "old:documents/../other",
    ],
)
def test_destination_overlap_and_traversal_rejected_before_listing(setup, target):
    env = setup
    with pytest.raises(migration.MigrationError):
        migration.preview(
            env.store,
            env.db,
            identity="d" * 32,
            target=target,
            expected_revision=env.store.revision,
        )
    assert not env.commands


def test_source_change_or_nonempty_destination_after_preview_never_overwrites(setup):
    env = setup
    plan = preview(env)
    env.files["new:documents"]["unrelated.txt"] = b"keep"
    failed = migration.start(env.store, env.db, plan["id"], background=False)
    assert failed["status"] == "failed" and not env.commands
    assert env.files["new:documents"]["unrelated.txt"] == b"keep"


def test_same_size_content_tampering_after_verification_blocks_switch(setup):
    env = setup
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    env.files["new:documents"]["report.txt"] = b"bad"
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    assert env.store.get("backup", "pairs")[0]["remote"] == "old:documents"


def test_vault_bindings_and_external_archives_are_explicitly_unsupported(setup):
    env = setup
    records = env.root / "server/device-vault/records"
    records.mkdir(parents=True)
    (records / "receipt.json").write_text(
        json.dumps({"identity": "d" * 32, "status": "ready"})
    )
    with pytest.raises(migration.MigrationError, match="Vault-Bindungen"):
        preview(env)
    (records / "receipt.json").unlink()
    env.store.update(
        lambda cfg: cfg["backup"]["pairs"][0].update(backup_dir="old:versions/{date}")
    )
    with pytest.raises(migration.MigrationError, match="Versionsablagen"):
        preview(env)


def test_expiry_stale_cutover_and_lock_contention_preserve_original(setup, monkeypatch):
    env = setup
    plan = preview(env)
    env.lease.held = True
    with pytest.raises(migration.MigrationBusy):
        migration.start(env.store, env.db, plan["id"], background=False)
    env.lease.held = False
    migration.start(env.store, env.db, plan["id"], background=False)
    env.store.update(lambda cfg: cfg.update(note="concurrent update"))
    with pytest.raises(ConfigConflictError):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    assert env.store.get("backup", "pairs")[0]["remote"] == "old:documents"
    monkeypatch.setattr(migration.time, "time", lambda: plan["expires_at"] + 1)
    assert migration.get_plan(env.db, plan["id"])["status"] == "expired"


def test_auth_and_csrf_are_required_for_migration_api(setup, monkeypatch):
    env = setup
    monkeypatch.setattr(api_migrations, "get_config", lambda: env.store)
    monkeypatch.setattr(api_migrations, "get_db", lambda: env.db)
    app = FastAPI()
    app.include_router(api_migrations.router)
    client = TestClient(app)
    body = {
        "identity": "d" * 32,
        "target": "new:documents",
        "expected_revision": env.store.revision,
    }
    assert client.get("/api/migrations").status_code == 401
    app.dependency_overrides[require_auth] = lambda: "admin"
    assert client.post("/api/migrations/preview", json=body).status_code == 403
    client.cookies.set("rclone_sync_csrf", "token")
    client.headers["X-CSRF-Token"] = "token"
    response = client.post("/api/migrations/preview", json=body)
    assert response.status_code == 200 and response.json()["status"] == "preview"


def test_cutover_is_async_job_with_pollable_terminal_result(setup, monkeypatch):
    env = setup
    plan = preview(env)
    copied = migration.start(env.store, env.db, plan["id"], background=False)

    class InlineThread:
        def __init__(self, *, target, args, **_kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(migration.threading, "Thread", InlineThread)
    response = migration.start_switch(
        env.store,
        env.db,
        plan["id"],
        expected_revision=plan["config_revision"],
        expected_generation=account_generation(env.store.snapshot()),
    )
    assert response["status"] == "switching"
    actual = migration.get_plan(env.db, plan["id"])
    assert actual["status"] == "switched" and actual["copy_job_id"] == copied["job_id"]
    assert actual["job_id"] != copied["job_id"]
    assert env.db.job_get(actual["job_id"])["status"] == "ok"
    assert not env.lease.held


def test_config_cutover_metadata_is_recovered_after_db_write_failure(
    setup, monkeypatch
):
    env = setup
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    save = migration._save

    def fail_after_commit(database, value, **kwargs):
        if value["status"] == "switched":
            raise OSError("DB temporarily unavailable")
        return save(database, value, **kwargs)

    monkeypatch.setattr(migration, "_save", fail_after_commit)
    with pytest.raises(OSError):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    assert env.store.get("backup", "pairs")[0]["remote"] == "new:documents"
    migration.recover_cutovers(env.store, env.db)
    assert migration.get_plan(env.db, plan["id"])["status"] == "switched"
    assert (
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )["status"]
        == "switched"
    )


def test_revoked_reauthentication_blocks_config_commit(setup):
    env = setup
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    with pytest.raises(ConfigConflictError, match="Passwortbestätigung"):
        migration.switch(
            env.store,
            env.db,
            plan["id"],
            expected_revision=plan["config_revision"],
            expected_generation="revoked",
        )
    assert env.store.get("backup", "pairs")[0]["remote"] == "old:documents"


@pytest.mark.parametrize(
    "rows",
    [
        [{"Path": "../escape", "Size": 1}],
        [{"Path": "link", "Size": 1, "IsLink": True}],
        [{"Path": "file", "Size": True}],
        [{"Path": "file", "Size": 1}, {"Path": "file", "Size": 1}],
    ],
)
def test_inventory_rejects_unsafe_or_ambiguous_provider_metadata(monkeypatch, rows):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(rows), "")

    monkeypatch.setattr(migration, "run_command", run)
    with pytest.raises(migration.MigrationError):
        migration._inventory("cloud:folder", cancellable=False)
    assert calls[0][0] == ["rclone", "lsjson", "--recursive", "--", "cloud:folder"]
    assert calls[0][1]["max_output_bytes"] == migration.MAX_OUTPUT


def test_readback_requires_exact_sha256_manifest_without_missing_or_extra_paths(
    monkeypatch,
):
    monkeypatch.setattr(
        migration,
        "run_command",
        lambda command, **_kw: subprocess.CompletedProcess(
            command, 0, "a" * 64 + "  unexpected.txt\n", ""
        ),
    )
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration._hashes("cloud:folder", {"report.txt": {"directory": False}})


def _historical_snapshot(env, number=1):
    from app import recovery_snapshots as snapshots

    config = env.store.snapshot()
    pair = config["backup"]["pairs"][0]
    point_id = "full-" + f"{number:032x}"
    root = snapshots.snapshot_root(config) / point_id
    (root / "data").mkdir(parents=True)
    (root / "data/report.txt").write_bytes(b"old")
    value = {
        "schema": "sicherpfad-full-snapshot-v1",
        "id": point_id,
        "identity": pair.get("id") or pair["name"],
        "pair": pair["name"],
        "kind": "full",
        "created_at": number,
        "fingerprint": snapshots.pair_fingerprint(pair),
        "complete": True,
        "entries": [
            {
                "path": "report.txt",
                "size": 3,
                "sha256": hashlib.sha256(b"old").hexdigest(),
            }
        ],
        "directories": [],
        "files": 1,
        "total_bytes": 3,
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(value))
    return point_id, manifest


def test_verified_cutover_keeps_immutable_historical_snapshot_search_and_restore(
    setup, monkeypatch
):
    from app import file_catalog, recovery_snapshots as snapshots

    env = setup
    point_id, manifest = _historical_snapshot(env)
    original_bytes = manifest.read_bytes()
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    pair = config["backup"]["pairs"][0]
    loaded = snapshots.load_manifest(config, pair, point_id)
    assert loaded["historical_binding"] and loaded["current_restore_proof"] is False
    assert manifest.read_bytes() == original_bytes
    monkeypatch.setattr(
        file_catalog,
        "_available_points",
        lambda *_a: ([{"id": point_id, "kind": "full"}], False),
    )
    assert (
        file_catalog.search_files(config, "report")["items"][0]["point_id"] == point_id
    )
    job_id = env.db.job_start("recovery")
    result = snapshots.run_snapshot_restore(
        env.db, config, pair, point_id=point_id, max_total_mb=1, job_id=job_id
    )
    assert result["verified"] and result["historical_binding"]
    assert result["current_restore_proof"] is False


@pytest.mark.parametrize(
    "field,value",
    [("remote", "unknown:other"), ("local", "/other/source"), ("id", "e" * 32)],
)
def test_unverified_target_source_or_identity_edits_never_inherit_lineage(
    setup, field, value
):
    from app import recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    changed = {
        **config["backup"]["pairs"][0],
        field: value,
        "migration_previous_fingerprints": [
            snapshots.pair_fingerprint(config["backup"]["pairs"][0])
        ],
    }
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_manifest(config, changed, point_id)


def test_signed_lineage_detects_tampering_and_multiple_cutovers_remain_transitive(
    setup,
):
    from app import migration_lineage, recovery_snapshots as snapshots

    env = setup
    first_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    second_id, _manifest = _historical_snapshot(env, 2)
    env.files["third:documents"] = {}
    second = migration.preview(
        env.store,
        env.db,
        identity="d" * 32,
        target="third:documents",
        expected_revision=env.store.revision,
    )
    migration.start(env.store, env.db, second["id"], background=False)
    migration.switch(
        env.store, env.db, second["id"], expected_revision=second["config_revision"]
    )
    config = env.store.snapshot()
    pair = config["backup"]["pairs"][0]
    assert all(
        snapshots.load_manifest(config, pair, point)["historical_binding"]
        for point in (first_id, second_id)
    )
    # Ledger survives ephemeral plan retention; it is not sourced from runtime rows.
    env.db.runtime_delete(migration.STATE_KEY)
    assert snapshots.load_manifest(config, pair, first_id)["historical_binding"]
    ledger = migration_lineage._path(config)
    value = json.loads(ledger.read_text())
    value["bindings"][0]["ancestors"].append("f" * 64)
    ledger.write_text(json.dumps(value))
    with pytest.raises(snapshots.SnapshotError, match="historische"):
        snapshots.load_manifest(config, pair, first_id)


def test_pending_lineage_does_not_grant_alias_until_exact_config_commit_replays(
    setup, monkeypatch
):
    from app import migration_lineage, recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    commit = migration_lineage.commit

    def interrupt(config, *args, **kwargs):
        if config["backup"]["pairs"][0]["remote"] == "new:documents":
            raise OSError("crash after config commit")
        return commit(config, *args, **kwargs)

    monkeypatch.setattr(migration_lineage, "commit", interrupt)
    with pytest.raises(OSError):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    config = env.store.snapshot()
    assert config["backup"]["pairs"][0]["remote"] == "new:documents"
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)
    monkeypatch.setattr(migration_lineage, "commit", commit)
    migration.recover_cutovers(env.store, env.db)
    config = env.store.snapshot()
    assert snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)[
        "historical_binding"
    ]


def test_legacy_raw_pair_receives_stable_id_without_losing_old_snapshot(setup):
    from app import recovery_snapshots as snapshots

    env = setup

    def legacy(cfg):
        cfg["backup"]["pairs"][0].pop("id")

    env.store.update(legacy)
    point_id, _manifest = _historical_snapshot(env)
    plan = migration.preview(
        env.store,
        env.db,
        identity="Documents",
        target="new:documents",
        expected_revision=env.store.revision,
    )
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    pair = config["backup"]["pairs"][0]
    assert len(pair["id"]) == 32
    assert snapshots.load_manifest(config, pair, point_id)["historical_binding"]


def test_rotating_session_secret_keeps_historical_backup_lineage(setup):
    from app import migration_lineage, recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    key = migration_lineage._key_path(config).read_bytes()
    env.store.update(
        lambda cfg: cfg["web"].update(
            secret_key=secrets.token_urlsafe(48), session_version=2
        )
    )
    changed = env.store.snapshot()
    assert snapshots.load_manifest(changed, changed["backup"]["pairs"][0], point_id)[
        "historical_binding"
    ]
    assert migration_lineage._key_path(changed).read_bytes() == key


@pytest.mark.parametrize("damage", ["missing", "tampered", "short"])
def test_missing_or_modified_lineage_key_fails_closed_without_regeneration(
    setup, damage
):
    from app import migration_lineage, recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    key = migration_lineage._key_path(config)
    if damage == "missing":
        key.unlink()
    else:
        key.write_bytes(b"broken" if damage == "short" else b"x" * 32)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)
    if damage == "missing":
        assert not key.exists()
        with pytest.raises(ValueError, match="nicht neu erzeugt"):
            migration_lineage._key(config, create=True)


@pytest.mark.parametrize("name", ["ledger", "key"])
def test_lineage_never_follows_symlink_to_ledger_or_signing_key(setup, name):
    from app import migration_lineage, recovery_snapshots as snapshots
    import os

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )
    config = env.store.snapshot()
    path = (
        migration_lineage._path(config)
        if name == "ledger"
        else migration_lineage._key_path(config)
    )
    outside = env.root / "outside"
    outside.write_bytes(path.read_bytes())
    try:
        path.unlink()
        os.symlink(outside, path)
    except OSError:
        pytest.skip("Host does not grant symlink creation")
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)


def test_prepared_lineage_and_generic_config_edit_cannot_authorize_old_binding(
    setup, monkeypatch
):
    from app import recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    save = env.store._save_unlocked
    monkeypatch.setattr(
        env.store,
        "_save_unlocked",
        lambda: (_ for _ in ()).throw(OSError("config disk failure")),
    )
    with pytest.raises(OSError):
        migration.switch(
            env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
        )
    monkeypatch.setattr(env.store, "_save_unlocked", save)

    def arbitrary_edit(cfg):
        cfg["backup"]["pairs"][0]["remote"] = "new:documents"
        cfg["unverified_aliases"] = ["anything"]

    env.store.update(arbitrary_edit)
    migration.recover_cutovers(env.store, env.db)
    config = env.store.snapshot()
    with pytest.raises(snapshots.SnapshotError):
        snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)
    assert migration.get_plan(env.db, plan["id"])["status"] != "switched"


def test_historical_snapshot_read_does_not_initialize_global_database(
    setup, monkeypatch
):
    from app import db, recovery_snapshots as snapshots

    env = setup
    point_id, _manifest = _historical_snapshot(env)
    plan = preview(env)
    migration.start(env.store, env.db, plan["id"], background=False)
    migration.switch(
        env.store, env.db, plan["id"], expected_revision=plan["config_revision"]
    )

    def forbidden():
        raise AssertionError(
            "Global database must not initialize while reading manifests"
        )

    monkeypatch.setattr(db, "get_db", forbidden)
    config = env.store.snapshot()
    assert snapshots.load_manifest(config, config["backup"]["pairs"][0], point_id)[
        "historical_binding"
    ]
