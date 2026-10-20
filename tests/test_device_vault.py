from __future__ import annotations

import hashlib
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

    def fail(*_args):
        raise RuntimeError("dummy failure")

    monkeypatch.setattr(device_vault, "_complete_upload_locked", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="dummy failure"):
            device_vault.complete_upload(AuditDatabase(), config, record["id"])
        assert record["id"] not in device_vault._TRANSFER_LOCKS
