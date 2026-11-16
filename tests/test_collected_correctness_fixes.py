"""Behavioral regressions for the collected correctness fixes; all I/O is local."""

import asyncio
import copy
import csv
import hashlib
import io
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException, Request

from app import (
    config_store,
    db as db_module,
    device_vault,
    maintenance,
    ransomware_guard,
)
from app.config_store import Config
from app.config_validation import validate_config
from app.db import Database
from app.jobs import bitrot_scrub, boot_drill, selective_restore
from app.routes import api_config, api_crypt, api_jobs, api_maintenance, api_test
from app.security import require_csrf


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    pairs = []
    for index, name in enumerate(("First", "Second"), 1):
        local = tmp_path / f"source-{index}"
        local.mkdir()
        pairs.append(
            {
                "id": str(index) * 32,
                "name": name,
                "local": str(local),
                "remote": f"cloud:{name}",
                "direction": "push",
                "mode": "copy",
            }
        )
    raw = {
        "web": {
            "username": "admin",
            "secret_key": "collected-correctness-fixture-0123456789-ABCDEFGHIJKLMN",
            "session_version": 1,
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
            "recovery_dir": str(tmp_path / "recovery"),
            "device_vault_dir": str(tmp_path / "vault"),
        },
        "backup": {
            "enabled": True,
            "default_schedule": "manual",
            "filter_file": str(tmp_path / "data" / "filters.txt"),
            "pairs": pairs,
            "jobs": [
                {
                    "id": "a" * 32,
                    "name": "Both paths",
                    "data_path_ids": [pair["id"] for pair in pairs],
                    "schedule": "manual",
                    "enabled": True,
                    "execution_mode": "sequential",
                }
            ],
        },
    }
    normalized, _warnings = validate_config(raw)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(normalized), encoding="utf-8")
    store = Config(config_path)
    database = Database(tmp_path / "fixture.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db_module, "_db", database)
    return store, database


def _request(*, cookie="", method="POST"):
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "headers": [(b"cookie", cookie.encode("latin-1"))] if cookie else [],
            "client": ("192.0.2.25", 12345),
        }
    )


@pytest.mark.parametrize(
    "cookie,supplied",
    [
        ("ascii-nonce", "ü" * 24),
        ("ü" * 24, "ascii-nonce"),
        ("ü" * 24, "ü" * 24),
    ],
)
def test_non_ascii_csrf_is_rejected_without_comparison_error(cookie, supplied):
    with pytest.raises(HTTPException) as denied:
        require_csrf(_request(), csrf_cookie=cookie, csrf_header=supplied)
    assert denied.value.status_code == 403
    require_csrf(_request(), csrf_cookie="matching-ascii", csrf_header="matching-ascii")


def test_login_routes_reject_non_ascii_nonce_before_authentication(
    local_state, monkeypatch
):
    from app import main

    monkeypatch.setattr(
        main,
        "_authenticate_login",
        lambda *_args: pytest.fail(
            "Malformed login nonce must not reach password verification"
        ),
    )
    request = _request(cookie=f"{main.LOGIN_CSRF_COOKIE}={'ü' * 24}")
    browser = main.login_submit(
        request,
        username="admin",
        password="dummy-password",
        login_csrf="ü" * 24,
        next="/security",
    )
    assert browser.status_code == 303
    assert browser.headers["location"] == "/login?next=/security&error=csrf"
    native = main.native_login_submit(
        request,
        main.NativeLoginRequest(
            username="admin", password="dummy-password", login_csrf="ü" * 24
        ),
    )
    assert native.status_code == 403
    assert json.loads(native.body) == {"status": "csrf_failed"}


@pytest.mark.parametrize("name", ["=SUM(1,2)", " +1+2", "\t@SUM(1,2)", "-1+2"])
def test_csv_job_labels_are_text_and_numeric_columns_remain_numeric(local_state, name):
    _store, database = local_state
    job_id = database.job_start("backup", definition_name=name)
    database.job_finish(job_id, "error", {"error": "fixture", "count": 3})
    stored_name = database.job_get(job_id)["definition_name"]
    response = api_jobs.export_jobs_csv(kind="backup", status=None, q="", limit=20)

    async def collect():
        return "".join(
            [
                chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                async for chunk in response.body_iterator
            ]
        )

    rows = list(csv.reader(io.StringIO(asyncio.run(collect()).lstrip("\ufeff"))))
    assert rows[1][0] == str(job_id)
    assert rows[1][3] == "'" + stored_name
    assert json.loads(rows[1][10])["count"] == 3


def test_backup_preview_revision_captures_the_actual_queued_snapshot(
    local_state, monkeypatch
):
    store, _database = local_state
    _snapshot, revision = store.snapshot_with_revision()
    queued = []

    def queue(**kwargs):
        store.update(
            lambda data: data["backup"]["pairs"][0].update(remote="cloud:changed")
        )
        queued.append(kwargs)
        return {"ok": True, "job_id": 7}

    monkeypatch.setattr(api_jobs, "_queue_backup", queue)
    result = api_jobs.run_backup(
        dry_run=False,
        pairs="First",
        payload=api_jobs.BackupRunPayload(expected_revision=revision),
        user="admin",
    )
    assert result["job_id"] == 7
    assert queued[0]["config_revision"] == revision
    assert queued[0]["config_snapshot"]["backup"]["pairs"][0]["remote"] == "cloud:First"
    assert store.get("backup", "pairs")[0]["remote"] == "cloud:changed"


def test_stale_backup_preview_cannot_queue_any_work(local_state, monkeypatch):
    store, _database = local_state
    old_revision = store.revision
    store.update(
        lambda data: data["backup"].update(
            max_parallel=int(data["backup"]["max_parallel"]) + 1
        )
    )
    assert store.revision != old_revision
    monkeypatch.setattr(
        api_jobs,
        "_queue_backup",
        lambda **_kw: pytest.fail("Stale preview queued work"),
    )
    with pytest.raises(HTTPException) as denied:
        api_jobs.run_backup(
            dry_run=False,
            pairs="First",
            payload=api_jobs.BackupRunPayload(expected_revision=old_revision),
            user="admin",
        )
    assert denied.value.status_code == 409


def test_retry_of_a_failed_dry_run_stays_a_dry_run(local_state, monkeypatch):
    store, database = local_state
    definition = store.get("backup", "jobs")[0]
    job_id = database.job_start(
        "backup",
        definition_id=definition["id"],
        config_revision=store.revision,
        dry_run=True,
    )
    database.job_finish(
        job_id, "error", {"error": "fixture dry-run failure", "dry_run": True}
    )
    queued = []
    monkeypatch.setattr(
        api_jobs, "_queue_backup", lambda **kw: queued.append(kw) or {"job_id": 8}
    )
    result = api_jobs.retry_job(job_id, dry_run=False, user="admin")
    assert result["retry_of_job_id"] == job_id
    assert queued[0]["dry_run"] is True
    assert queued[0]["pairs_filter"] == ["First", "Second"]


def test_cancel_before_batch_start_terminalizes_the_reserved_job(
    local_state, monkeypatch
):
    store, database = local_state
    snapshot, revision = store.snapshot_with_revision()
    definition = snapshot["backup"]["jobs"][0]
    attempt = {"name": "First", "history_key": "rclone:id:first", "trigger": "web"}
    first = database.job_start(
        "backup", definition_id=definition["id"], attempts=[attempt]
    )
    batch_id = database.job_batch_create(
        specs=[
            {
                "definition": definition,
                "pair_names": ["First"],
                "history_keys": {"First": attempt["history_key"]},
                "attempts": [attempt],
            }
        ],
        snapshot=snapshot,
        config_revision=revision,
        dry_run=False,
        first_job_id=first,
    )
    database.job_batch_request_cancel()
    released = []
    scope = SimpleNamespace(release=lambda: released.append(True))
    monkeypatch.setattr(api_jobs.rclone_job, "is_cancelled", lambda: False)
    monkeypatch.setattr(
        api_jobs,
        "_run_backup_thread",
        lambda *_a, **_kw: pytest.fail("Cancelled batch started"),
    )
    assert api_jobs._locks["backup"].acquire(blocking=False)
    api_jobs._run_definition_batch_thread(batch_id, scope)
    assert database.job_get(first)["status"] == "cancelled"
    assert database.job_running("backup") is None
    assert database.job_batch_get(batch_id)["state"] == "cancelled"
    assert released == [True] and not api_jobs._locks["backup"].locked()


def test_filter_character_classes_and_exact_filename_whitespace():
    result = api_config.evaluate_rclone_filter_rules(
        "- /file[0-9].txt\n- /[!a-c].bin\n- /normal.txt\n",
        [
            "file2.txt",
            "fileA.txt",
            "d.bin",
            "b.bin",
            "normal.txt",
            " normal.txt",
            "normal.txt ",
            "dir\\normal.txt",
        ],
    )
    assert result["ok"] is True
    assert [row["included"] for row in result["results"]] == [
        False,
        True,
        False,
        True,
        False,
        True,
        True,
        True,
    ]
    assert [row["path"] for row in result["results"]][-3:] == [
        " normal.txt",
        "normal.txt ",
        "dir\\normal.txt",
    ]
    malformed = api_config.evaluate_rclone_filter_rules("- /broken[abc\n", ["brokena"])
    assert malformed["ok"] is False and malformed["parse_errors"]


@pytest.mark.skipif(
    os.name != "posix", reason="Filter publication uses POSIX descriptor permissions"
)
def test_persisted_filter_save_survives_audit_failure(local_state, monkeypatch):
    _store, database = local_state
    original = api_config.get_filter_file()
    monkeypatch.setattr(
        database,
        "audit_add",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("fixture audit")),
    )
    result = api_config.save_filter_file(
        api_config.FilterPayload(content="- *.tmp\n", revision=original["revision"])
    )
    assert result["ok"] is True
    assert api_config.get_filter_file()["content"] == "- *.tmp\n"


def test_calendar_preview_accepts_full_day_and_rejects_impossible_dates():
    result = api_config.preview_schedule(
        api_config.SchedulePreviewRequest(
            expression="* * * * *", timezone="UTC", count=1441
        )
    )
    times = [entry["timestamp"] for entry in result["next_runs"]]
    assert len(times) == 1441 and times[-1] - times[0] == 86400
    with pytest.raises(HTTPException) as denied:
        api_config.preview_schedule(
            api_config.SchedulePreviewRequest(expression="0 0 31 2 *")
        )
    assert denied.value.status_code == 422


def test_inline_pair_validation_preserves_other_pairs_and_job_references(
    local_state, monkeypatch
):
    store, _database = local_state
    before = store.snapshot()
    draft = {**before["backup"]["pairs"][1], "remote": "cloud:changed-draft"}
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "cloud:\n" if command[1] == "listremotes" else '{"count":1,"bytes":4}',
            "",
        )

    monkeypatch.setattr(api_test.subprocess, "run", run)
    result = api_test.test_rclone(api_test.RcloneTest(pair=draft))
    assert result["ok"] is True and result["tested_unsaved"] is True
    assert calls[1][-1] == "cloud:changed-draft"
    assert store.snapshot() == before


@pytest.mark.parametrize("remote_source", [False, True])
def test_explicit_pair_checks_local_targets_and_unreadable_cloud_sources_without_global_remote(
    local_state, tmp_path, monkeypatch, remote_source
):
    store, _database = local_state
    destination = tmp_path / "local-target"
    destination.mkdir()
    source = "missing-cloud:source" if remote_source else str(tmp_path / "source-1")
    pair = {
        "name": "Local target",
        "local": source,
        "remote": str(destination),
        "direction": "push",
        "mode": "copy",
    }
    store.update(
        lambda data: data["backup"].update(
            pairs=[pair], jobs=[], rclone_remote="obsolete"
        )
    )
    tested = []

    def run(command, **_kwargs):
        if command[1] == "listremotes":
            return subprocess.CompletedProcess(command, 0, "", "")
        tested.append(command[-1])
        code = 3 if command[-1] == "missing-cloud:source" else 0
        return subprocess.CompletedProcess(
            command, code, '{"count":1,"bytes":4}', "not found" if code else ""
        )

    monkeypatch.setattr(api_test.subprocess, "run", run)
    result = api_test.test_rclone(api_test.RcloneTest(pair_index=0))
    assert result["ok"] is (not remote_source)
    assert tested == (
        [str(destination), source] if remote_source else [str(destination)]
    )
    if remote_source:
        assert result["local_exists"] is False and "fehlgeschlagen" in result["message"]


def test_snapshot_diff_recursively_redacts_secrets_inside_list_values(local_state):
    store, _database = local_state
    old_secret, new_secret = "fixture-old-password", "fixture-new-password"
    store.update(
        lambda data: data.update(
            custom_integrations=[
                {
                    "name": "Demo",
                    "password": old_secret,
                    "headers": {"Authorization": "Bearer fixture-old-token"},
                }
            ]
        )
    )
    snapshot = api_maintenance.create_config_snapshot()["snapshot"]
    store.update(
        lambda data: data.update(
            custom_integrations=[
                {
                    "name": "Changed",
                    "password": new_secret,
                    "headers": {"Authorization": "Bearer fixture-new-token"},
                }
            ]
        )
    )
    result = api_maintenance.diff_config_snapshot(snapshot["name"])
    rendered = json.dumps(result)
    assert old_secret not in rendered and new_secret not in rendered
    assert "fixture-old-token" not in rendered and "fixture-new-token" not in rendered
    assert any(change["path"] == "custom_integrations" for change in result["changes"])
    assert "Demo" in rendered and "Changed" in rendered


def test_late_snapshot_revision_conflict_does_not_revoke_devices(
    local_state, monkeypatch
):
    store, database = local_state
    snapshot = api_maintenance.create_config_snapshot()["snapshot"]
    revision = store.revision
    token = "ab" * 32
    database.push_device_upsert(token, "production")
    monkeypatch.setattr(
        api_maintenance, "require_reauthentication", lambda *_a: "confirmed"
    )
    original_write = api_maintenance._write_snapshot

    def write_then_race(*args, **kwargs):
        result = original_write(*args, **kwargs)
        store.update(lambda data: data["backup"].update(max_parallel=3))
        return result

    monkeypatch.setattr(api_maintenance, "_write_snapshot", write_then_race)
    with pytest.raises(HTTPException) as denied:
        api_maintenance.restore_config_snapshot(
            _request(),
            api_maintenance.SnapshotRestore(
                name=snapshot["name"],
                current_password="dummy-confirmation",
                expected_revision=revision,
            ),
            user="admin",
        )
    assert denied.value.status_code == 409
    assert database.push_device_exists(token)
    assert store.get("backup", "max_parallel") == 3
    assert store.get("web", "session_version") == 1


def test_log_enumeration_ignores_aliases_even_when_the_target_is_inside_root(tmp_path):
    root = tmp_path / "logs"
    nested = root / "real"
    nested.mkdir(parents=True)
    log = nested / "kept.log"
    log.write_text("fixture", encoding="utf-8")
    try:
        (root / "alias.log").symlink_to(log)
        (root / "directory-alias").symlink_to(nested, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require host support")
    assert list(maintenance.iter_logs(root)) == [log]


@pytest.mark.parametrize(
    "chunks,expected",
    [
        ([b'{"ret', b'urn":', b"{}}\n"], True),
        ([b'{"error":{"desc":"return"}}\n'], False),
        ([b'{"return":{},"error":{}}\n'], False),
        ([b'{"return":'], False),
    ],
)
def test_guest_ping_requires_complete_success_json(chunks, expected):
    class Socket:
        def __init__(self):
            self.chunks = iter(chunks)
            self.sent = b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def sendall(self, payload):
            self.sent += payload

        def recv(self, _count):
            return next(self.chunks, b"")

    sock = Socket()
    assert boot_drill._guest_ping(lambda _path: sock, "unused-fixture.sock") is expected
    assert json.loads(sock.sent) == {"execute": "guest-ping"}


@pytest.mark.skipif(
    os.name != "posix", reason="Boot workspace owner records contain POSIX UID"
)
def test_boot_cleanup_only_removes_a_workspace_with_matching_owner_marker(
    tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    owned, unowned, foreign = [
        scratch / ("sicherpfad-bootdrill-" + name)
        for name in ("owned", "user-data", "mismatched")
    ]
    for directory in (owned, unowned, foreign):
        directory.mkdir()
        (directory / "retained.txt").write_text("fixture", encoding="utf-8")
    for directory, inode_delta in ((owned, 0), (foreign, 1)):
        info = directory.stat()
        (directory / ".sicherpfad-owner").write_text(
            json.dumps(
                {
                    "device": info.st_dev,
                    "inode": info.st_ino + inode_delta,
                    "uid": info.st_uid,
                }
            ),
            encoding="ascii",
        )
    monkeypatch.setattr(boot_drill, "client_path", lambda: "dummy-pbs-client")
    monkeypatch.setattr(boot_drill.shutil, "which", lambda _name: "dummy-qemu")
    monkeypatch.setattr(
        boot_drill.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0)
    )
    monkeypatch.setattr(boot_drill, "notify", lambda *_a, **_kw: None)
    listing = [
        {
            "backup-type": "vm",
            "backup-id": "100",
            "backup-time": 100,
            "files": [{"filename": "drive.img.fidx", "size": 1}],
        }
    ]
    result = boot_drill.run_boot_drill(
        config_snapshot={
            "paths": {"temp_dir": str(scratch)},
            "pbs": {
                "enabled": True,
                "repository": "user@fixture:store",
                "boot_drill": {"group": "vm/100"},
            },
        },
        run=lambda command, **_kw: subprocess.CompletedProcess(
            command, 0, json.dumps(listing), ""
        ),
        popen=lambda *_a, **_kw: pytest.fail(
            "Disk budget must stop before starting QEMU"
        ),
    )
    assert result["ok"] is False and "Platz" in result["error"]
    assert not owned.exists() and unowned.is_dir() and foreign.is_dir()
    assert result["stale_cleaned"] == 1


@pytest.mark.parametrize(
    "files",
    [
        [],
        [
            {
                "path": "too-large.bin",
                "size": 2 * 1024**2,
                "modtime": "2020-01-01T00:00:00Z",
            }
        ],
    ],
)
def test_zero_bitrot_sample_is_not_a_success(local_state, tmp_path, monkeypatch, files):
    store, database = local_state
    monkeypatch.setattr(
        bitrot_scrub,
        "_list_files",
        lambda *_a, **_kw: {"files": files, "scanned": len(files), "truncated": False},
    )
    monkeypatch.setattr(bitrot_scrub, "_filter_args", lambda *_a: [])
    monkeypatch.setattr(bitrot_scrub, "is_cancelled", lambda: False)
    monkeypatch.setattr(
        bitrot_scrub,
        "_hashsum",
        lambda *_a, **_kw: pytest.fail("Empty sample cannot hash data"),
    )
    settings = {**bitrot_scrub.bitrot_scrub_settings({}), "max_total_mb": 1}
    result = bitrot_scrub._scrub_pair(
        store.get("backup", "pairs")[0],
        settings=settings,
        cfg=store.snapshot(),
        db=database,
        log_file=tmp_path / "dummy.log",
    )
    assert result["ok"] is False and result["skipped"] is True
    assert result["sampled"] == 0 and result["error"]


def test_canary_initialization_preserves_existing_user_data(local_state, tmp_path):
    store, database = local_state
    pair = store.get("backup", "pairs")[0]
    target = Path(pair["local"]) / "important.txt"
    target.write_bytes(b"irreplaceable fixture content")
    with pytest.raises(ValueError, match="vorhandene Datei"):
        ransomware_guard.init_canary(pair, filename=target.name, database=database)
    assert target.read_bytes() == b"irreplaceable fixture content"
    assert not list(target.parent.glob(".sicherpfad-canary.*.tmp"))
    first = ransomware_guard.init_canary(
        pair, filename=".fixture-canary", database=database
    )
    payload = (target.parent / ".fixture-canary").read_bytes()
    second = ransomware_guard.init_canary(
        pair, filename=".fixture-canary", database=database
    )
    assert first == second
    assert (target.parent / ".fixture-canary").read_bytes() == payload


def test_concurrent_recovery_metadata_updates_preserve_every_record(local_state):
    _store, database = local_state
    barrier = threading.Barrier(6)

    def save(index):
        barrier.wait(timeout=10)
        selective_restore._save_record(
            database, {"id": f"recovery-{index}", "status": "ready"}
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(save, range(6)))
    records = database.runtime_get(selective_restore._STATE_KEY, [])
    assert {record["id"] for record in records} == {
        f"recovery-{index}" for index in range(6)
    }


@pytest.mark.skipif(
    os.name != "posix", reason="Literal backslash filename is a POSIX case"
)
def test_recovery_uses_exact_selected_filenames(local_state, monkeypatch):
    store, database = local_state
    config = store.snapshot()
    pair = config["backup"]["pairs"][0]
    contents = {
        " leading.txt": b"leading",
        "trailing.txt ": b"trailing",
        "literal\\name.txt": b"backslash",
    }
    observed = []

    def run(command, *, timeout):
        if "--files-from-raw" in command:
            listing = Path(command[command.index("--files-from-raw") + 1])
            observed.append(listing.read_text(encoding="utf-8").splitlines())
        output = ""
        if command[1] == "lsjson":
            output = json.dumps(
                [
                    {
                        "Path": name,
                        "Size": len(data),
                        "Hashes": {"SHA-256": hashlib.sha256(data).hexdigest()},
                    }
                    for name, data in contents.items()
                ]
            )
        elif command[1] == "copy":
            for name, data in contents.items():
                (Path(command[-1]) / name).write_bytes(data)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(selective_restore, "_run", run)
    monkeypatch.setattr(selective_restore, "notify", lambda *_a, **_kw: None)
    job = database.job_start("recovery")
    result = selective_restore.run_selective_restore(
        database, config, pair, list(contents), max_total_mb=1, job_id=job
    )
    assert result["status"] == "ready" and result["verified"] is True
    assert observed and all(selection == list(contents) for selection in observed)
    data_root = Path(result["staging_path"])
    assert {item.name: item.read_bytes() for item in data_root.iterdir()} == contents


def test_changed_recovery_root_does_not_discard_retained_metadata(
    local_state, tmp_path
):
    store, database = local_state
    config = store.snapshot()
    original = Path(config["paths"]["recovery_dir"]) / "recovery-99"
    (original / "data").mkdir(parents=True)
    (original / "data" / "retained.txt").write_bytes(b"retained")
    record = {
        "id": "recovery-99",
        "status": "ready",
        "staging_path": str(original / "data"),
    }
    selective_restore._save_record(database, record)
    moved = copy.deepcopy(config)
    moved["paths"]["recovery_dir"] = str(tmp_path / "different-recovery")
    with pytest.raises(ValueError, match="Ablageort"):
        selective_restore.remove_staging(database, moved, record["id"])
    assert database.runtime_get(selective_restore._STATE_KEY) == [record]
    assert (original / "data" / "retained.txt").read_bytes() == b"retained"
    assert selective_restore.remove_staging(database, config, record["id"]) is True
    assert database.runtime_get(selective_restore._STATE_KEY) == []


def _vault_upload(config, *, payload=b"fixture-photo", filename="photo.heic"):
    pair = config["backup"]["pairs"][0]
    return device_vault.create_upload(
        config,
        pair=pair,
        filename=filename,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type="photo",
        device_name="Fixture iPhone",
        target_root=pair["remote"],
    )


@pytest.fixture
def vault_transport(local_state, monkeypatch):
    store, database = local_state
    copies = []

    def copy_and_verify(blob, target, expected, *, timeout):
        content = blob.read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        copies.append((target, content))

    monkeypatch.setattr(device_vault, "_copy_and_verify_remote", copy_and_verify)
    monkeypatch.setattr(device_vault, "notify", lambda *_a, **_kw: None)
    return store.snapshot(), database, copies


def test_vault_dedup_rechecks_content_and_repairs_corrupt_same_size_blob(
    vault_transport,
):
    config, database, copies = vault_transport
    payload = b"small verified fixture"
    first = _vault_upload(config, payload=payload)
    device_vault.append_chunk(config, first["id"], offset=0, payload=payload)
    device_vault.queue_completion(config, first["id"])
    assert (
        device_vault.complete_upload(database, config, first["id"])["status"] == "ready"
    )
    blob, _name = device_vault.download_blob(config, first["id"])
    blob.write_bytes(b"x" * len(payload))
    retry = _vault_upload(config, payload=payload)
    assert retry["status"] == "receiving" and retry["deduplicated"] is False
    device_vault.append_chunk(config, retry["id"], offset=0, payload=payload)
    device_vault.queue_completion(config, retry["id"])
    assert (
        device_vault.complete_upload(database, config, retry["id"])["status"] == "ready"
    )
    assert blob.read_bytes() == payload and [
        content for _target, content in copies
    ] == [payload, payload]


def test_vault_long_utf8_filename_keeps_identity_with_a_publishable_target(
    vault_transport,
):
    config, database, copies = vault_transport
    filename = "備" * 220 + ".heic"
    record = _vault_upload(config, filename=filename)
    assert record["filename"] == filename
    assert len(record["target_relative"].rsplit("/", 1)[1].encode("utf-8")) <= 240
    device_vault.append_chunk(config, record["id"], offset=0, payload=b"fixture-photo")
    device_vault.queue_completion(config, record["id"])
    assert (
        device_vault.complete_upload(database, config, record["id"])["status"]
        == "ready"
    )
    assert copies


def test_vault_chunk_retry_is_idempotent_and_conflicting_data_is_rejected(
    vault_transport,
):
    config, _database, _copies = vault_transport
    record = _vault_upload(config, payload=b"abcdef")
    device_vault.append_chunk(config, record["id"], offset=0, payload=b"abc")
    retry = device_vault.append_chunk(config, record["id"], offset=0, payload=b"abc")
    assert retry["received"] == 3 and retry["status"] == "receiving"
    with pytest.raises(device_vault.VaultError):
        device_vault.append_chunk(config, record["id"], offset=0, payload=b"xyz")
    done = device_vault.append_chunk(config, record["id"], offset=3, payload=b"def")
    final_retry = device_vault.append_chunk(
        config, record["id"], offset=3, payload=b"def"
    )
    assert done["status"] == final_retry["status"] == "uploaded"
    part = device_vault._part_path(device_vault.vault_root(config), record["id"])
    assert part.read_bytes() == b"abcdef"


def test_vault_rejects_a_changed_cloud_target_before_copying(vault_transport):
    config, database, copies = vault_transport
    record = _vault_upload(config)
    device_vault.append_chunk(config, record["id"], offset=0, payload=b"fixture-photo")
    device_vault.queue_completion(config, record["id"])
    config["backup"]["pairs"][0]["remote"] = "other-cloud:replacement"
    result = device_vault.complete_upload(database, config, record["id"])
    assert result["status"] == "error" and result["verified"] is False
    assert copies == []


def test_vault_verified_ready_survives_audit_failure(vault_transport, monkeypatch):
    config, database, copies = vault_transport
    record = _vault_upload(config)
    device_vault.append_chunk(config, record["id"], offset=0, payload=b"fixture-photo")
    device_vault.queue_completion(config, record["id"])
    monkeypatch.setattr(
        database,
        "audit_add",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("fixture audit")),
    )
    result = device_vault.complete_upload(database, config, record["id"])
    persisted = device_vault.upload_status(config, record["id"])
    assert result["status"] == persisted["status"] == "ready"
    assert result["verified"] is True and result["error"] is None
    assert (
        device_vault.download_blob(config, record["id"])[0].read_bytes()
        == b"fixture-photo"
    )
    assert len(copies) == 1


@pytest.mark.parametrize("scenario", ["empty", "authenticated", "wrong-key", "no-byte"])
def test_crypt_verification_requires_an_authenticated_file_byte(monkeypatch, scenario):
    files = (
        []
        if scenario == "empty"
        else [{"Path": "existing document.txt", "Size": 4, "IsDir": False}]
    )
    commands = []

    def listing(command):
        commands.append(command)
        return files, False

    def run(command, **_kwargs):
        commands.append(command)
        if command[1] == "lsd":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        assert command == [
            "rclone",
            "cat",
            "--head",
            "1",
            "--",
            "encrypted:existing document.txt",
        ]
        return subprocess.CompletedProcess(
            command,
            1 if scenario == "wrong-key" else 0,
            b"x" if scenario == "authenticated" else b"",
            b"dummy decryption error",
        )

    monkeypatch.setattr(api_crypt, "_remote_listing", listing)
    monkeypatch.setattr(api_crypt.subprocess, "run", run)
    if scenario in {"wrong-key", "no-byte"}:
        with pytest.raises(HTTPException) as denied:
            api_crypt.verify("encrypted")
        assert denied.value.status_code == 502
    else:
        result = api_crypt.verify("encrypted")
        assert result["ok"] is True and result["verified"] is (
            scenario == "authenticated"
        )
    assert commands[1] == [
        "rclone",
        "lsjson",
        "--files-only",
        "--max-depth",
        "1",
        "--",
        "encrypted:",
    ]
    assert len(commands) == (2 if scenario == "empty" else 3)
