import copy
import hashlib
import io
import json
import multiprocessing
import queue
from pathlib import Path

import pytest

from app import device_vault
from app import handover_rescue as rescue
from app.routes.api_recovery import encrypted_handover


class AuditDB:
    def audit_add(self, *args, **kwargs):
        return 1


def _concurrent_rescue_import(
    config, envelope, before_write, release_write, validated, result
):
    """Pause one process after its existence check, before publishing a receipt."""
    if before_write is not None:
        original_save = device_vault._save_record

        def paused_save(root, record):
            before_write.set()
            if not release_write.wait(20):
                raise RuntimeError("Timed out waiting to publish the rescue receipt")
            return original_save(root, record)

        device_vault._save_record = paused_save
    if validated is not None:
        original_validate = rescue.validate_inventory

        def signal_validated(package):
            inventory = original_validate(package)
            validated.set()
            return inventory

        rescue.validate_inventory = signal_validated
    try:
        imported = rescue.import_handover(
            config, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
        )
        if before_write is None:
            device_vault.download_blob(config, imported["items"][0]["id"])
        result.put(("ok", imported["imported"], imported["items"][0]["id"]))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))


def _failing_rescue_import(config, envelope, continue_import, result):
    original_save = device_vault._save_record
    writes = 0

    def fail_after_publication(root, record):
        nonlocal writes
        writes += 1
        if writes > 1:
            raise OSError("Simulated disk write failure")
        saved = original_save(root, record)
        result.put(("published", record["id"]))
        if not continue_import.wait(20):
            raise RuntimeError("Timed out waiting for the parallel restore")
        return saved

    device_vault._save_record = fail_after_publication
    try:
        rescue.import_handover(
            config, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
        )
        result.put(("unexpected_success",))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))


def fixture(tmp_path, monkeypatch):
    target = tmp_path / "cloud"
    pair = {
        "id": "old-photos",
        "name": "Fotos",
        "direction": "push",
        "local": str(tmp_path / "photos"),
        "remote": str(target),
    }
    config = {
        "paths": {"data_dir": str(tmp_path / "old-server")},
        "backup": {"pairs": [pair]},
        "auth": {"password": "never-export-this"},
    }
    payload = b"recover me without the old server"
    record = device_vault.create_upload(
        config,
        pair=pair,
        filename="photo.jpg",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type="photo",
        device_name="iPhone",
        target_root=str(target),
    )
    device_vault.append_chunk(config, record["id"], offset=0, payload=payload)
    device_vault.queue_completion(config, record["id"])
    monkeypatch.setattr(device_vault, "notify", lambda *args, **kwargs: None)
    device_vault.complete_upload(AuditDB(), config, record["id"])
    package = {
        "rescue_inventory": rescue.portable_inventory(config, include_paths=False)
    }
    envelope = encrypted_handover(package, "correct-rescue-passphrase")
    new_pair = {**pair, "id": "new-photos"}
    fresh = {
        "paths": {"data_dir": str(tmp_path / "new-server")},
        "backup": {"pairs": [new_pair]},
    }
    return config, fresh, record, envelope, payload


def test_fresh_server_import_maps_target_and_recovers_verified_bytes(
    tmp_path, monkeypatch
):
    old, fresh, record, envelope, payload = fixture(tmp_path, monkeypatch)
    unchanged = copy.deepcopy(fresh)
    preview = rescue.preview_handover(fresh, envelope, "correct-rescue-passphrase")
    assert preview["records"] == 1 and not preview["credentials_included"]
    assert "never-export-this" not in json.dumps(
        rescue.decrypt_handover(envelope, "correct-rescue-passphrase")
    )
    imported = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )
    assert imported["imported"] == 1
    item = imported["items"][0]
    assert isinstance(item["updated_at"], (int, float))
    assert item["status"] == "remote" and item["verified"] is False
    assert fresh == unchanged
    assert not (
        device_vault.vault_root(fresh) / "blobs" / item["sha256"][:2] / item["sha256"]
    ).exists()
    downloaded, _filename = device_vault.download_blob(fresh, item["id"])
    assert downloaded.read_bytes() == payload
    assert device_vault.upload_status(fresh, item["id"])["verified"] is True
    again = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )
    assert again["imported"] == 0 and again["already_present"] == 1


def test_preview_requests_mapping_only_for_paths_with_portable_files(
    tmp_path, monkeypatch
):
    _old, fresh, _record, envelope, _payload = fixture(tmp_path, monkeypatch)
    package = rescue.decrypt_handover(envelope, "correct-rescue-passphrase")
    package["rescue_inventory"]["data_paths"].append(
        {"identity": "without-vault-files", "name": "Rezepte"}
    )
    package_envelope = encrypted_handover(package, "correct-rescue-passphrase")
    preview = rescue.preview_handover(
        fresh, package_envelope, "correct-rescue-passphrase"
    )
    assert [row["identity"] for row in preview["data_paths"]] == ["old-photos"]
    mappings = {row["identity"]: "new-photos" for row in preview["data_paths"]}
    result = rescue.import_handover(
        fresh, package_envelope, "correct-rescue-passphrase", mappings
    )
    assert result["imported"] == 1


def test_rescue_rejects_wrong_passphrase_bad_kdf_and_traversal(tmp_path, monkeypatch):
    _old, fresh, _record, envelope, _payload = fixture(tmp_path, monkeypatch)
    with pytest.raises(rescue.HandoverError, match="Passphrase"):
        rescue.preview_handover(fresh, envelope, "wrong-long-passphrase")
    with pytest.raises(rescue.HandoverError, match="Verschlüsselung"):
        rescue.decrypt_handover(
            {**envelope, "iterations": 999_999_999}, "correct-rescue-passphrase"
        )
    package = rescue.decrypt_handover(envelope, "correct-rescue-passphrase")
    package["rescue_inventory"]["records"][0]["target_relative"] = (
        "Sicherpfad/../../secret"
    )
    malicious = encrypted_handover(package, "correct-rescue-passphrase")
    with pytest.raises(rescue.HandoverError, match="Pfad"):
        rescue.import_handover(
            fresh, malicious, "correct-rescue-passphrase", {"old-photos": "new-photos"}
        )
    assert not (Path(fresh["paths"]["data_dir"]) / "device-vault").exists()


@pytest.mark.parametrize("field", ["identity", "source_type"])
@pytest.mark.parametrize("value", [[], {}])
def test_malformed_encrypted_inventory_is_rejected_before_import(
    tmp_path, monkeypatch, field, value
):
    _old, fresh, _record, envelope, _payload = fixture(tmp_path, monkeypatch)
    package = rescue.decrypt_handover(envelope, "correct-rescue-passphrase")
    package["rescue_inventory"]["records"][0][field] = value
    malformed = encrypted_handover(package, "correct-rescue-passphrase")

    for action in (
        lambda: rescue.preview_handover(fresh, malformed, "correct-rescue-passphrase"),
        lambda: rescue.import_handover(
            fresh, malformed, "correct-rescue-passphrase", {"old-photos": "new-photos"}
        ),
    ):
        with pytest.raises(rescue.HandoverError):
            action()
    assert not (Path(fresh["paths"]["data_dir"]) / "device-vault").exists()


def test_concurrent_rescue_import_preserves_verified_receipt(tmp_path, monkeypatch):
    _old, fresh, _record, envelope, payload = fixture(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    before_write = context.Event()
    release_write = context.Event()
    second_validated = context.Event()
    first_results = context.Queue()
    second_results = context.Queue()
    first = context.Process(
        target=_concurrent_rescue_import,
        args=(fresh, envelope, before_write, release_write, None, first_results),
    )
    second = context.Process(
        target=_concurrent_rescue_import,
        args=(fresh, envelope, None, None, second_validated, second_results),
    )
    first.start()
    try:
        assert before_write.wait(15), "The first importer never reached publication"
        second.start()
        assert second_validated.wait(15), (
            "The second importer never validated the package"
        )
        # Without a process lock the second importer creates and verifies the
        # same receipt while the first still holds an unpublished stale copy.
        try:
            second_result = second_results.get(timeout=5)
        except queue.Empty:
            second_result = None
        release_write.set()
        first_result = first_results.get(timeout=15)
        if second_result is None:
            second_result = second_results.get(timeout=15)
        first.join(15)
        second.join(15)
        assert first.exitcode == 0 and second.exitcode == 0
        assert first_result[0] == second_result[0] == "ok"
        assert first_result[1] + second_result[1] == 1
        assert first_result[2] == second_result[2]
        record = device_vault.upload_status(fresh, first_result[2])
        assert record["status"] == "ready" and record["verified"] is True
        restored, _name = device_vault.download_blob(fresh, record["id"])
        assert restored.read_bytes() == payload
    finally:
        release_write.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(5)
        first_results.close()
        second_results.close()


def test_partial_import_keeps_parallel_verified_receipt_and_resumes(
    tmp_path, monkeypatch
):
    _old, fresh, _record, envelope, payload = fixture(tmp_path, monkeypatch)
    package = rescue.decrypt_handover(envelope, "correct-rescue-passphrase")
    second = copy.deepcopy(package["rescue_inventory"]["records"][0])
    second["id"] = "second-file"
    package["rescue_inventory"]["records"].append(second)
    envelope = encrypted_handover(package, "correct-rescue-passphrase")
    context = multiprocessing.get_context("spawn")
    continue_import = context.Event()
    result = context.Queue()
    importer = context.Process(
        target=_failing_rescue_import, args=(fresh, envelope, continue_import, result)
    )
    importer.start()
    try:
        published = result.get(timeout=15)
        assert published[0] == "published"
        recovered, _name = device_vault.download_blob(fresh, published[1])
        assert recovered.read_bytes() == payload
        continue_import.set()
        failure = result.get(timeout=15)
        importer.join(15)
        assert importer.exitcode == 0
        assert failure[0:2] == ("error", "HandoverError")
        receipt = device_vault.upload_status(fresh, published[1])
        assert receipt["status"] == "ready" and receipt["verified"] is True

        resumed = rescue.import_handover(
            fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
        )
        assert resumed["already_present"] == 1 and resumed["imported"] == 1
        receipt = device_vault.upload_status(fresh, published[1])
        assert receipt["status"] == "ready" and receipt["verified"] is True
        assert len(resumed["items"]) == 2
    finally:
        continue_import.set()
        if importer.is_alive():
            importer.terminate()
        importer.join(5)
        result.close()


def test_cloud_corruption_is_not_released_or_cached(tmp_path, monkeypatch):
    old, fresh, record, envelope, payload = fixture(tmp_path, monkeypatch)
    imported = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )
    item = imported["items"][0]
    target = Path(fresh["backup"]["pairs"][0]["remote"]) / item["target_relative"]
    target.write_bytes(b"x" * len(payload))
    with pytest.raises(device_vault.VaultError, match="SHA-256"):
        device_vault.download_blob(fresh, item["id"])
    assert device_vault.upload_status(fresh, item["id"])["verified"] is False
    assert not list((device_vault.vault_root(fresh) / "uploads").glob("rescue-*.part"))


def test_existing_receipt_recovers_after_only_local_blob_was_lost(
    tmp_path, monkeypatch
):
    old, _fresh, record, _envelope, payload = fixture(tmp_path, monkeypatch)
    blob, _name = device_vault.download_blob(old, record["id"])
    blob.unlink()
    restored, _name = device_vault.download_blob(old, record["id"])
    assert restored.read_bytes() == payload


def test_import_requires_explicit_current_target_mapping(tmp_path, monkeypatch):
    _old, fresh, _record, envelope, _payload = fixture(tmp_path, monkeypatch)
    for mapping in (
        {},
        {"old-photos": "missing"},
        {"old-photos": "new-photos", "extra": "new-photos"},
    ):
        with pytest.raises(rescue.HandoverError, match="zuordnen"):
            rescue.import_handover(
                fresh, envelope, "correct-rescue-passphrase", mapping
            )


def test_reconfigured_target_requires_fresh_explicit_import(tmp_path, monkeypatch):
    _old, fresh, _record, envelope, _payload = fixture(tmp_path, monkeypatch)
    item = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )["items"][0]
    fresh["backup"]["pairs"][0]["remote"] = str(tmp_path / "different")
    with pytest.raises(device_vault.VaultError, match="geändert"):
        device_vault.download_blob(fresh, item["id"])


def test_fresh_mapping_rebinds_removed_identity_at_the_same_target(
    tmp_path, monkeypatch
):
    _old, fresh, _record, envelope, payload = fixture(tmp_path, monkeypatch)
    original = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )["items"][0]
    fresh["backup"]["pairs"][0]["id"] = "replacement-photos"
    expected_config = copy.deepcopy(fresh)
    replaced = rescue.import_handover(
        fresh,
        envelope,
        "correct-rescue-passphrase",
        {"old-photos": "replacement-photos"},
    )
    assert replaced["imported"] == 1
    replacement = replaced["items"][0]
    assert replacement["id"] != original["id"]
    assert replacement["identity"] == "replacement-photos"
    recovered, _name = device_vault.download_blob(fresh, replacement["id"])
    assert recovered.read_bytes() == payload
    assert device_vault.upload_status(fresh, original["id"])["identity"] == "new-photos"
    assert fresh == expected_config
    repeated = rescue.import_handover(
        fresh,
        envelope,
        "correct-rescue-passphrase",
        {"old-photos": "replacement-photos"},
    )
    assert repeated["imported"] == 0 and repeated["already_present"] == 1
    assert repeated["items"][0]["id"] == replacement["id"]


def test_missing_server_blob_streams_cloud_bytes_through_size_and_hash_verification(
    tmp_path, monkeypatch
):
    _old, fresh, _record, envelope, payload = fixture(tmp_path, monkeypatch)
    fresh["backup"]["pairs"][0]["remote"] = "trusted-cloud:/Photos"
    item = rescue.import_handover(
        fresh, envelope, "correct-rescue-passphrase", {"old-photos": "new-photos"}
    )["items"][0]
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdout = io.BytesIO(payload)
            self.returncode = None

        def wait(self, timeout):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

    monkeypatch.setattr(device_vault.subprocess, "Popen", Process)

    restored, _name = device_vault.download_blob(fresh, item["id"])

    assert restored.read_bytes() == payload
    assert commands == [
        ["rclone", "cat", "--", "trusted-cloud:/Photos/" + item["target_relative"]]
    ]
    assert device_vault.upload_status(fresh, item["id"])["verified"] is True
