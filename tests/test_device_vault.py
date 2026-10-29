from __future__ import annotations

import hashlib
import os
import multiprocessing

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import device_vault


class AuditDatabase:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def audit_add(self, event_type: str, *, actor: str, details: dict) -> int:
        self.events.append((event_type, details))
        return len(self.events)


def _config(tmp_path: Path) -> dict:
    return {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "device_vault_dir": str(tmp_path / "vault"),
        },
        "backup": {
            "timeout_hours": 0.1,
            "pairs": [
                {
                    "id": "photos",
                    "name": "Fotos",
                    "local": str(tmp_path / "photos"),
                    "remote": str(tmp_path / "target"),
                    "direction": "push",
                }
            ],
        },
    }


def _create(tmp_path: Path, payload: bytes) -> tuple[dict, dict]:
    config = _config(tmp_path)
    pair = {
        "id": "photos",
        "name": "Fotos",
        "direction": "push",
        "remote": str(tmp_path / "target"),
    }
    config["backup"]["pairs"] = [pair]
    record = device_vault.create_upload(
        config,
        pair=pair,
        filename="Urlaub.heic",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type="photo",
        device_name="Olivers iPhone",
        target_root=str(tmp_path / "target"),
        local_only=True,
    )
    return config, record


def test_resumable_upload_is_verified_and_restorable(tmp_path: Path, monkeypatch):
    payload = b"verified-device-photo" * 80_000
    config, record = _create(tmp_path, payload)
    first = payload[: device_vault.MAX_CHUNK_BYTES]
    second = payload[len(first) :]

    status = device_vault.append_chunk(config, record["id"], offset=0, payload=first)
    assert status["received"] == len(first)
    assert status["status"] == "receiving"
    status = device_vault.append_chunk(
        config, record["id"], offset=len(first), payload=second
    )
    assert status["status"] == "uploaded"

    monkeypatch.setattr(device_vault, "notify", lambda *_args, **_kwargs: None)
    database = AuditDatabase()
    assert device_vault.queue_completion(config, record["id"])["status"] == "queued"
    completed = device_vault.complete_upload(database, config, record["id"])

    assert completed["status"] == "ready"
    assert completed["verified"] is True
    restored, filename = device_vault.download_blob(config, record["id"])
    assert restored.read_bytes() == payload
    assert filename == "Urlaub.heic"
    target = tmp_path / "target" / completed["target_relative"]
    assert target.read_bytes() == payload
    assert database.events[0][0] == "device_vault_ready"


def test_content_hash_deduplicates_second_upload(tmp_path: Path, monkeypatch):
    payload = b"same-content" * 100
    config, first = _create(tmp_path, payload)
    device_vault.append_chunk(config, first["id"], offset=0, payload=payload)
    monkeypatch.setattr(device_vault, "notify", lambda *_args, **_kwargs: None)
    device_vault.queue_completion(config, first["id"])
    device_vault.complete_upload(AuditDatabase(), config, first["id"])

    _config_again, second = _create(tmp_path, payload)

    assert second["deduplicated"] is True
    assert second["received"] == len(payload)
    assert second["status"] == "uploaded"


def test_restore_rejects_a_corrupted_local_blob(tmp_path: Path, monkeypatch):
    payload = b"verified-content"
    config, record = _create(tmp_path, payload)
    device_vault.append_chunk(config, record["id"], offset=0, payload=payload)
    monkeypatch.setattr(device_vault, "notify", lambda *_args, **_kwargs: None)
    device_vault.queue_completion(config, record["id"])
    device_vault.complete_upload(AuditDatabase(), config, record["id"])
    blob, _filename = device_vault.download_blob(config, record["id"])
    blob.write_bytes(b"x" * len(payload))

    with pytest.raises(device_vault.VaultError, match="beschädigt"):
        device_vault.download_blob(config, record["id"])


def test_rejects_wrong_offset_and_unsafe_filename(tmp_path: Path):
    payload = b"content"
    config, record = _create(tmp_path, payload)

    try:
        device_vault.append_chunk(config, record["id"], offset=1, payload=payload)
    except device_vault.VaultError as exc:
        assert "Versatz" in str(exc)
    else:
        raise AssertionError("wrong offsets must fail")

    try:
        device_vault.safe_filename("../secret")
    except device_vault.VaultError:
        pass
    else:
        raise AssertionError("unsafe filename must fail")


@pytest.mark.parametrize(
    "filename",
    [
        "Notiz\t12.txt",
        "line\x1ffeed.txt",
        "delete\x7f.txt",
        "trailing.txt\t",
        "\tleading.txt",
        "control\x85.txt",
    ],
)
def test_filename_rejects_all_control_characters(filename):
    with pytest.raises(device_vault.VaultError, match="Dateiname"):
        device_vault.safe_filename(filename)


def test_vault_target_uses_remote_for_bisync_and_requires_local_consent(tmp_path):
    assert (
        device_vault.target_root({"local": "/source", "remote": "cloud:"}) == "cloud:"
    )
    assert (
        device_vault.target_root(
            {"direction": "bisync", "local": "/source", "remote": "cloud:"}
        )
        == "cloud:"
    )
    assert (
        device_vault.target_root(
            {"direction": "pull", "local": "/target", "remote": "cloud:"}
        )
        == "/target"
    )
    with pytest.raises(device_vault.VaultError, match="ausdrücklich"):
        device_vault.create_upload(
            _config(tmp_path),
            pair={},
            filename="note.txt",
            size=1,
            sha256="a" * 64,
            source_type="file",
            device_name="iPhone",
            target_root=str(tmp_path / "target"),
        )


def test_bisync_receipts_keep_their_original_target_and_endpoint_binding(tmp_path):
    pair = {
        "id": "photos",
        "local": str(tmp_path / "legacy-local"),
        "remote": "cloud:/photos",
        "direction": "bisync",
    }
    config = {"backup": {"pairs": [pair]}}
    for saved_target in (pair["remote"], pair["local"]):
        receipt = {
            "identity": "photos",
            "target_root": saved_target,
            "endpoint_binding": device_vault.endpoint_binding(pair),
        }
        device_vault._validate_destination(config, receipt)
        changed = {"backup": {"pairs": [{**pair, "remote": "other-cloud:/photos"}]}}
        with pytest.raises(device_vault.VaultError, match="Konfiguration neu laden"):
            device_vault._validate_destination(changed, receipt)


def test_local_publication_rejects_an_escaping_parent(tmp_path):
    boundary = tmp_path / "target"
    boundary.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    try:
        (boundary / "Sicherpfad").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require host support")
    blob = tmp_path / "blob"
    blob.write_bytes(b"fixture")
    with pytest.raises(device_vault.VaultError):
        device_vault._copy_and_verify_local(
            blob,
            str(boundary / "Sicherpfad" / "file"),
            hashlib.sha256(b"fixture").hexdigest(),
            base=str(boundary),
        )
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="Anchored directory I/O requires POSIX")
def test_parent_replacement_cannot_redirect_local_publication(tmp_path, monkeypatch):
    boundary = tmp_path / "target"
    parent = boundary / "Sicherpfad"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = tmp_path / "moved-original"
    blob = tmp_path / "blob"
    payload = b"anchored-vault-bytes"
    blob.write_bytes(payload)
    original_copy = device_vault.shutil.copyfileobj

    def replace_parent(source, destination, **kwargs):
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        return original_copy(source, destination, **kwargs)

    monkeypatch.setattr(device_vault.shutil, "copyfileobj", replace_parent)
    with pytest.raises(device_vault.VaultError):
        device_vault._copy_and_verify_local(
            blob,
            str(parent / "photo"),
            hashlib.sha256(payload).hexdigest(),
            base=str(boundary),
        )
    assert list(outside.iterdir()) == []
    assert (moved / "photo").read_bytes() == payload
    assert not list(moved.glob(".sicherpfad-*"))


@pytest.mark.skipif(os.name != "posix", reason="Anchored directory I/O requires POSIX")
def test_parent_replacement_cannot_redirect_local_restore(tmp_path, monkeypatch):
    boundary = tmp_path / "target"
    parent = boundary / "Sicherpfad"
    parent.mkdir(parents=True)
    (parent / "photo").write_bytes(b"expected")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "photo").write_bytes(b"untrusted")
    moved = tmp_path / "moved-original"
    original_open = device_vault.os.open
    replaced = False

    def replace_parent(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "photo" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(device_vault.os, "open", replace_parent)
    monkeypatch.setattr(
        device_vault.os,
        "supports_dir_fd",
        device_vault.os.supports_dir_fd | {replace_parent},
    )
    with device_vault._open_local_target(boundary, parent / "photo") as handle:
        assert handle.read() == b"expected"


def test_local_target_io_fails_closed_without_dirfd_support(tmp_path, monkeypatch):
    monkeypatch.setattr(device_vault.os, "supports_dir_fd", set())
    with pytest.raises(device_vault.VaultError, match="POSIX"):
        with device_vault._anchored_local_parent(tmp_path, tmp_path / "file"):
            pytest.fail("Unsupported hosts must not fall back to pathname I/O")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_regular_reader_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(device_vault.VaultError, match="reguläre"):
        device_vault._open_regular(fifo)


def test_local_copy_removal_keeps_target_and_shared_blob(tmp_path, monkeypatch):
    payload = b"retained-target"
    config, first = _create(tmp_path, payload)
    device_vault.append_chunk(config, first["id"], offset=0, payload=payload)
    device_vault.queue_completion(config, first["id"])
    monkeypatch.setattr(device_vault, "notify", lambda *args, **kwargs: None)
    ready = device_vault.complete_upload(AuditDatabase(), config, first["id"])
    assert ready["status"] == "ready"
    _, second = _create(tmp_path, payload)
    blob = device_vault._blob_path(device_vault.vault_root(config), first["sha256"])
    target = tmp_path / "target" / ready["target_relative"]
    assert device_vault.remove_local_copy(config, first["id"])["shared_blob_retained"]
    assert blob.exists() and target.read_bytes() == payload
    assert not device_vault.remove_local_copy(config, second["id"])[
        "shared_blob_retained"
    ]
    assert not blob.exists() and target.read_bytes() == payload


def test_local_mount_guard_is_checked_before_creating_a_vault_record(tmp_path):
    config = _config(tmp_path)
    pair = {
        "direction": "pull",
        "local": str(tmp_path / "absent"),
        "sentinel_file": ".mounted",
    }
    with pytest.raises(device_vault.VaultError, match="Sentinel"):
        device_vault.create_upload(
            config,
            pair=pair,
            filename="file.txt",
            size=1,
            sha256="a" * 64,
            source_type="file",
            device_name="iPhone",
            target_root=pair["local"],
            local_only=True,
        )
    assert not (tmp_path / "vault" / "records").exists()


@pytest.mark.parametrize("committed", [0, 4])
@pytest.mark.parametrize("last_chunk", [False, True])
def test_restart_resends_only_unacknowledged_bytes(
    tmp_path: Path, monkeypatch, committed: int, last_chunk: bool
):
    payload = b"abcdefghijklmno"
    config, record = _create(tmp_path, payload)
    upload_id = record["id"]
    if committed:
        device_vault.append_chunk(
            config, upload_id, offset=0, payload=payload[:committed]
        )
    root = device_vault.vault_root(config)
    part = device_vault._part_path(root, upload_id)
    chunk = payload[committed:] if last_chunk else payload[committed : committed + 3]
    save = device_vault._save_record

    def crash_before_receipt(*_args, **_kwargs):
        raise OSError("simulated termination after chunk fsync")

    monkeypatch.setattr(device_vault, "_save_record", crash_before_receipt)
    with pytest.raises(OSError, match="termination"):
        device_vault.append_chunk(config, upload_id, offset=committed, payload=chunk)
    assert part.read_bytes() == payload[:committed] + chunk
    assert device_vault._load_record(root, upload_id)["received"] == committed

    # A fresh status read performs the same reconciliation after a restart.
    monkeypatch.setattr(device_vault, "_save_record", save)
    status = device_vault.upload_status(config, upload_id)
    assert status["received"] == committed
    assert status["status"] == "receiving"
    assert part.read_bytes() == payload[:committed]
    device_vault.append_chunk(
        config, upload_id, offset=status["received"], payload=payload[committed:]
    )
    device_vault.queue_completion(config, upload_id)
    monkeypatch.setattr(device_vault, "notify", lambda *_a, **_k: None)
    completed = device_vault.complete_upload(AuditDatabase(), config, upload_id)
    assert completed["status"] == "ready"
    assert completed["verified"] is True
    assert (tmp_path / "target" / completed["target_relative"]).read_bytes() == payload


def test_append_reconciles_crash_even_without_status_request(tmp_path: Path):
    config, record = _create(tmp_path, b"abcdefgh")
    root = device_vault.vault_root(config)
    device_vault._part_path(root, record["id"]).write_bytes(b"uncommitted")
    status = device_vault.append_chunk(
        config, record["id"], offset=0, payload=b"abcdefgh"
    )
    assert status["received"] == 8
    assert status["status"] == "uploaded"
    assert device_vault._part_path(root, record["id"]).read_bytes() == b"abcdefgh"


def test_receipt_ahead_of_file_resumes_at_available_bytes(tmp_path: Path):
    config, record = _create(tmp_path, b"abcdefgh")
    device_vault.append_chunk(config, record["id"], offset=0, payload=b"abcd")
    root = device_vault.vault_root(config)
    device_vault._part_path(root, record["id"]).write_bytes(b"ab")
    status = device_vault.upload_status(config, record["id"])
    assert status["received"] == 2
    assert device_vault._load_record(root, record["id"])["received"] == 2
    device_vault.append_chunk(config, record["id"], offset=2, payload=b"cdefgh")
    assert device_vault.upload_status(config, record["id"])["status"] == "uploaded"


def _probe_upload_lock(root: str, upload_id: str, result) -> None:
    from app.file_lock import acquire, release
    import os

    fd = os.open(str(Path(root) / "locks" / f"{upload_id}.lock"), os.O_RDWR)
    try:
        try:
            acquire(fd, blocking=False)
        except BlockingIOError:
            result.put("blocked")
        else:
            release(fd)
            result.put("acquired")
    finally:
        os.close(fd)


def test_upload_receipt_lock_excludes_another_process(tmp_path: Path):
    config, record = _create(tmp_path, b"content")
    root = device_vault.vault_root(config)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    with device_vault._upload_record_lock(root, record["id"]):
        process = context.Process(
            target=_probe_upload_lock, args=(str(root), record["id"], result)
        )
        process.start()
        try:
            assert result.get(timeout=15) == "blocked"
            process.join(timeout=15)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=15)
    with device_vault._upload_record_lock(root, record["id"]):
        pass
    assert (root / "locks" / f"{record['id']}.lock").is_file()


def test_vault_restart_cleanup_preserves_resumable_upload_and_live_rescue(
    tmp_path: Path,
):
    from app.jobs.restore_workspace import create_workspace

    config, upload = _create(tmp_path, b"abcdefgh")
    device_vault.append_chunk(config, upload["id"], offset=0, payload=b"abcd")
    root = device_vault.vault_root(config)
    orphan, lease = create_workspace(
        root / "uploads", "restore-vault-rescue-", kind="vault-rescue"
    )
    (orphan / "data.part").write_bytes(b"private scratch")
    lease.release()
    live, live_lease = create_workspace(
        root / "uploads", "restore-vault-rescue-", kind="vault-rescue"
    )
    try:
        assert device_vault.cleanup_vault_scratch(config) == {"removed": 1, "failed": 0}
        assert not orphan.exists()
        assert live.exists()
        assert device_vault._part_path(root, upload["id"]).read_bytes() == b"abcd"
        assert device_vault.upload_status(config, upload["id"])["received"] == 4
    finally:
        live_lease.release()


def test_remote_copy_failure_does_not_start_download(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        device_vault.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "disk full"),
    )

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("failed uploads must not trigger a cloud download")

    monkeypatch.setattr(device_vault.subprocess, "Popen", unexpected_download)
    with pytest.raises(RuntimeError, match="disk full"):
        device_vault._copy_and_verify_remote(
            tmp_path / "blob", "cloud:/file", "a" * 64, timeout=1
        )


def test_identical_concurrent_uploads_both_finish(tmp_path: Path, monkeypatch):
    payload = b"same-photo-two-devices"
    config, first = _create(tmp_path, payload)
    _, second = _create(tmp_path, payload)
    root = device_vault.vault_root(config)
    # Exercise the same destination even if the two fixtures cross a second.
    second_record = device_vault._load_record(root, second["id"])
    second_record["target_relative"] = device_vault._load_record(root, first["id"])[
        "target_relative"
    ]
    device_vault._save_record(root, second_record)
    for record in (first, second):
        device_vault.append_chunk(config, record["id"], offset=0, payload=payload)
        device_vault.queue_completion(config, record["id"])
    monkeypatch.setattr(device_vault, "notify", lambda *_args, **_kwargs: None)
    arrived = threading.Barrier(2)
    original = device_vault._prepare_blob

    def prepare(*args):
        arrived.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(device_vault, "_prepare_blob", prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                device_vault.complete_upload, AuditDatabase(), config, record["id"]
            )
            for record in (first, second)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert all(result["status"] == "ready" for result in results), results
    assert sum(bool(result["deduplicated"]) for result in results) == 1
    assert not device_vault._TRANSFER_LOCKS
    for record in (first, second):
        blob, _ = device_vault.download_blob(config, record["id"])
        assert blob.read_bytes() == payload


def test_transfer_exception_releases_lock(tmp_path: Path, monkeypatch):
    config, record = _create(tmp_path, b"dummy")

    def fail(*_args, **_kwargs):
        raise RuntimeError("dummy failure")

    monkeypatch.setattr(device_vault, "_complete_upload_locked", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="dummy failure"):
            device_vault.complete_upload(AuditDatabase(), config, record["id"])
        assert record["id"] not in device_vault._TRANSFER_LOCKS
