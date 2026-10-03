from __future__ import annotations

import hashlib
import multiprocessing
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
        "backup": {"timeout_hours": 0.1},
    }


def _create(tmp_path: Path, payload: bytes) -> tuple[dict, dict]:
    config = _config(tmp_path)
    record = device_vault.create_upload(
        config,
        pair={"id": "photos", "name": "Fotos"},
        filename="Urlaub.heic",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type="photo",
        device_name="Olivers iPhone",
        target_root=str(tmp_path / "target"),
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
