"""Import a user's encrypted rescue inventory without importing server authority.

Only portable vault receipts are accepted. The user maps each old data-path ID
to an already configured and authenticated target on this server. Credentials,
absolute paths, rclone configuration and active jobs are never imported.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import device_vault
from .jobs.restore_test import _endpoints

# The encrypted base64 envelope must fit the server's 2 MiB request cap.
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_RECORDS = 5000


class HandoverError(ValueError):
    pass


def _identity(pair: Mapping[str, Any]) -> str:
    return str(pair.get("id") or pair.get("name") or "")


def _pairs(config: Mapping[str, Any]) -> list[dict]:
    return [
        dict(row)
        for row in (config.get("backup") or {}).get("pairs") or []
        if isinstance(row, Mapping)
    ]


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 1024
        or not value.startswith("Sicherpfad/")
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or path.as_posix() != value
    ):
        raise HandoverError("Ungültiger Geräte-Vault-Pfad in der Notfallakte")
    return value


def portable_inventory(config: Mapping[str, Any], *, include_paths: bool) -> dict:
    pairs = {_identity(pair): pair for pair in _pairs(config)}
    records = []
    for path in (device_vault.vault_root(config) / "records").glob("*.json"):
        if len(records) >= MAX_RECORDS:
            raise HandoverError("Zu viele Geräte-Dateien für eine Notfallakte")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pair = (
            pairs.get(str(value.get("identity") or ""))
            if isinstance(value, dict)
            else None
        )
        if (
            not pair
            or value.get("status") != "ready"
            or value.get("verified") is not True
        ):
            continue
        # An old receipt for a renamed target must not silently refer to the new one.
        if str(value.get("target_root") or "") != _endpoints(pair)[1]:
            continue
        records.append(
            {
                key: value.get(key)
                for key in (
                    "id",
                    "identity",
                    "filename",
                    "size",
                    "sha256",
                    "source_type",
                    "device_name",
                    "target_relative",
                    "created_at",
                    "completed_at",
                )
            }
        )
    return {
        "schema": "sicherpfad-rescue-inventory-v1",
        "records": records,
        "data_paths": [
            {
                "identity": identity,
                "name": str(pair.get("name") or ""),
                "target_hint": _endpoints(pair)[1] if include_paths else None,
            }
            for identity, pair in pairs.items()
        ],
        "credentials_included": False,
        "files_included": False,
    }


def decrypt_handover(envelope: Mapping[str, Any], passphrase: str) -> dict:
    if not 12 <= len(passphrase) <= 1024:
        raise HandoverError("Passphrase muss 12 bis 1024 Zeichen lang sein")
    allowed = {
        "schema",
        "kdf",
        "iterations",
        "cipher",
        "salt_b64",
        "nonce_b64",
        "ciphertext_b64",
        "plaintext_sha256",
    }
    if set(envelope) != allowed:
        raise HandoverError("Notfallakte enthält unbekannte oder fehlende Felder")
    if (
        envelope.get("schema") != "rclone-recovery-handover-v1"
        or envelope.get("kdf") != "PBKDF2-HMAC-SHA256"
        or envelope.get("cipher") != "AES-256-GCM"
        or envelope.get("iterations") != 600_000
    ):
        raise HandoverError("Nicht unterstützte Verschlüsselung der Notfallakte")
    decoded = {}
    for name, maximum in (
        ("salt_b64", 32),
        ("nonce_b64", 24),
        ("ciphertext_b64", (MAX_PACKAGE_BYTES + 16) * 2),
    ):
        raw = envelope.get(name)
        if not isinstance(raw, str) or len(raw) > maximum:
            raise HandoverError("Notfallakte ist zu groß oder beschädigt")
        try:
            decoded[name] = base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise HandoverError("Notfallakte ist beschädigt") from exc
    if (
        len(decoded["salt_b64"]) != 16
        or len(decoded["nonce_b64"]) != 12
        or len(decoded["ciphertext_b64"]) > MAX_PACKAGE_BYTES + 16
    ):
        raise HandoverError("Notfallakte ist zu groß oder beschädigt")
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=decoded["salt_b64"],
        iterations=600_000,
    ).derive(passphrase.encode("utf-8"))
    try:
        plaintext = AESGCM(key).decrypt(
            decoded["nonce_b64"],
            decoded["ciphertext_b64"],
            b"rclone-recovery-handover-v1",
        )
        package = json.loads(plaintext)
    except (InvalidTag, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HandoverError(
            "Notfallakte konnte nicht geöffnet werden; Passphrase und Datei prüfen"
        ) from exc
    if hashlib.sha256(plaintext).hexdigest() != envelope.get(
        "plaintext_sha256"
    ) or not isinstance(package, dict):
        raise HandoverError("Integritätsprüfung der Notfallakte ist fehlgeschlagen")
    return package


def validate_inventory(package: Mapping[str, Any]) -> dict:
    inventory = package.get("rescue_inventory")
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema") != "sicherpfad-rescue-inventory-v1"
    ):
        raise HandoverError(
            "Diese ältere Notfallakte enthält noch kein importierbares Geräte-Vault-Verzeichnis"
        )
    paths = inventory.get("data_paths")
    records = inventory.get("records")
    if (
        not isinstance(paths, list)
        or len(paths) > 500
        or not isinstance(records, list)
        or len(records) > MAX_RECORDS
    ):
        raise HandoverError("Ungültiges oder zu großes Rettungsverzeichnis")
    known = {}
    for row in paths:
        if not isinstance(row, dict):
            raise HandoverError("Ungültiger Datenweg in der Notfallakte")
        identity = row.get("identity")
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity) <= 128
            or identity in known
        ):
            raise HandoverError("Ungültige Datenweg-ID in der Notfallakte")
        known[identity] = {
            "identity": identity,
            "name": str(row.get("name") or "")[:128],
            "target_hint": str(row.get("target_hint") or "")[:1024] or None,
        }
    clean = []
    seen = set()
    for row in records:
        if not isinstance(row, dict) or row.get("identity") not in known:
            raise HandoverError("Geräte-Datei ist keinem Datenweg zugeordnet")
        identity = row["identity"]
        identifier = str(row.get("id") or "")
        if not re.fullmatch(r"[a-z0-9-]{1,80}", identifier) or identifier in seen:
            raise HandoverError("Ungültige Geräte-Datei-ID in der Notfallakte")
        seen.add(identifier)
        size, digest = row.get("size"), row.get("sha256")
        if (
            type(size) is not int
            or not 1 <= size <= device_vault.MAX_FILE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))
        ):
            raise HandoverError(
                "Ungültige Dateigröße oder Prüfsumme in der Notfallakte"
            )
        if row.get("source_type") not in {"photo", "file"}:
            raise HandoverError("Ungültiger Dateityp in der Notfallakte")
        try:
            filename = device_vault.safe_filename(str(row.get("filename") or ""))
        except device_vault.VaultError as exc:
            raise HandoverError(str(exc)) from exc
        clean.append(
            {
                "id": identifier,
                "identity": identity,
                "size": size,
                "sha256": digest,
                "filename": filename,
                "source_type": row["source_type"],
                "device_name": device_vault.safe_device_name(
                    str(row.get("device_name") or "iPhone")
                ),
                "target_relative": _safe_relative(
                    str(row.get("target_relative") or "")
                ),
            }
        )
    return {"data_paths": list(known.values()), "records": clean}


def preview_handover(
    config: Mapping[str, Any], envelope: Mapping[str, Any], passphrase: str
) -> dict:
    inventory = validate_inventory(decrypt_handover(envelope, passphrase))
    return {
        "schema": "sicherpfad-rescue-preview-v1",
        "records": len(inventory["records"]),
        "total_bytes": sum(row["size"] for row in inventory["records"]),
        "data_paths": [
            row
            for row in inventory["data_paths"]
            if any(
                record["identity"] == row["identity"] for record in inventory["records"]
            )
        ],
        "available_targets": [
            {
                "identity": _identity(pair),
                "name": str(pair.get("name") or ""),
                "target": _endpoints(pair)[1],
            }
            for pair in _pairs(config)
        ],
        "credentials_included": False,
        "config_will_change": False,
        "instructions": "Cloud-Zugang auf diesem Server einrichten, Datenwege zuordnen und danach eine Datei zurückholen. Jede Datei wird vor der Freigabe mit SHA-256 geprüft.",
    }


def import_handover(
    config: Mapping[str, Any],
    envelope: Mapping[str, Any],
    passphrase: str,
    mappings: Mapping[str, str],
) -> dict:
    inventory = validate_inventory(decrypt_handover(envelope, passphrase))
    current = {_identity(pair): pair for pair in _pairs(config)}
    required = {row["identity"] for row in inventory["records"]}
    if set(mappings) != required or any(
        target not in current for target in mappings.values()
    ):
        raise HandoverError(
            "Jeden enthaltenen Datenweg ausdrücklich einem bestehenden Sicherungsziel zuordnen"
        )
    root = device_vault.vault_root(config)
    prepared = []
    for row in inventory["records"]:
        pair = current[mappings[row["identity"]]]
        target = _endpoints(pair)[1]
        if not target:
            raise HandoverError("Zugeordneter Datenweg besitzt kein Sicherungsziel")
        signature = hashlib.sha256(
            json.dumps(
                [row["id"], row["sha256"], target, row["target_relative"]]
            ).encode()
        ).hexdigest()
        identifier = f"rescue-{signature[:40]}"
        path = device_vault._record_path(root, identifier)
        if path.exists():
            existing = device_vault._load_record(root, identifier)
            if existing.get("rescue_signature") != signature:
                raise HandoverError(
                    "Eine vorhandene Datei kollidiert mit der Notfallakte"
                )
            prepared.append((path, existing, False))
            continue
        record = {
            **row,
            "id": identifier,
            "identity": _identity(pair),
            "pair": str(pair.get("name") or ""),
            "status": "remote",
            "verified": False,
            "received": 0,
            "deduplicated": False,
            "target_root": target,
            "created_at": time.time(),
            "error": None,
            "rescue_signature": signature,
            "origin_id": row["id"],
        }
        prepared.append((path, record, True))
    written = []
    try:
        with device_vault._LOCK:
            for path, record, should_write in prepared:
                if should_write:
                    if path.exists():
                        raise HandoverError(
                            "Notfallimport wurde bereits parallel gestartet"
                        )
                    record.update(device_vault._save_record(root, record))
                    written.append(path)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "imported": len(written),
        "already_present": len(prepared) - len(written),
        "items": [
            device_vault._public_record(record) for _path, record, _new in prepared
        ],
        "config_changed": False,
        "next_step": "Eine Geräte-Datei aus der Bibliothek zurückholen; die Cloud-Kopie wird vollständig geprüft.",
    }
