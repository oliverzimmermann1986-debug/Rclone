import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from app import device_vault
from app import handover_rescue as rescue
from app.routes.api_recovery import encrypted_handover


class AuditDB:
    def audit_add(self, *args, **kwargs):
        return 1


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
