"""Verschlüsselter Konfigurations-Bootstrap-Export und Disaster-Recovery-Restore.

Erstellt ein mit AES-256-GCM und PBKDF2-HMAC-SHA256 (600.000 Iterationen)
verschlüsseltes Bootstrap-Paket der Sicherpfad-Konfiguration (samt Filterdatei
und Portable-Vault-Inventar), das bei Konfigurationsänderungen automatisch in
ein lokales Zielverzeichnis geschrieben oder per CLI wiederhergestellt werden kann.
"""

from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import __version__
from .secret_redaction import REDACTED, redact_secrets

logger = logging.getLogger(__name__)

BOOTSTRAP_SCHEMA = "sicherpfad-bootstrap-v1"
BOOTSTRAP_AAD = b"sicherpfad-bootstrap-v1"
KDF_ITERATIONS = 600_000


class BootstrapExportError(ValueError):
    """Fehler beim Erstellen oder Entschlüsseln eines Bootstrap-Pakets."""


def _derive_key(
    secret_material: str, salt: bytes, iterations: int = KDF_ITERATIONS
) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=max(100_000, int(iterations)),
    )
    return kdf.derive(secret_material.encode("utf-8"))


def _sanitized_config_for_bootstrap(
    config_snapshot: Mapping[str, Any], *, include_secrets: bool = False
) -> dict[str, Any]:
    cfg = copy.deepcopy(dict(config_snapshot))
    cfg.pop("_revision", None)
    if not include_secrets:
        web = cfg.get("web")
        if isinstance(web, dict):
            for key in ("password", "password_hash", "secret_key"):
                if key in web:
                    web[key] = REDACTED
        pbs = cfg.get("pbs")
        if isinstance(pbs, dict) and "password" in pbs:
            pbs["password"] = REDACTED
        redacted = redact_secrets(cfg)
        return redacted if isinstance(redacted, dict) else {}
    return cfg


def create_encrypted_bootstrap_bundle(
    config_snapshot: Mapping[str, Any],
    *,
    passphrase: str,
    revision: str = "",
    include_secrets: bool = False,
    include_rclone_conf: bool = False,
) -> dict[str, Any]:
    """Erstellt ein AES-256-GCM-verschlüsseltes Bootstrap-Paket."""
    clean_pass = str(passphrase or "").strip()
    if len(clean_pass) < 12:
        raise BootstrapExportError(
            "Passphrase für den Bootstrap-Export muss mindestens 12 Zeichen lang sein"
        )
    paths = config_snapshot.get("paths") if isinstance(config_snapshot, Mapping) else {}
    backup = (
        config_snapshot.get("backup") if isinstance(config_snapshot, Mapping) else {}
    )
    filter_content = ""
    if isinstance(backup, Mapping) and backup.get("filter_file"):
        try:
            fp = Path(str(backup["filter_file"]))
            if fp.is_file() and not fp.is_symlink() and fp.stat().st_size <= 512 * 1024:
                filter_content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            filter_content = ""

    rclone_conf_content = ""
    if include_rclone_conf:
        try:
            from .rclone_args import resolve_rclone_config_path

            rc_path = resolve_rclone_config_path()
            if (
                rc_path is not None
                and rc_path.is_file()
                and not rc_path.is_symlink()
                and rc_path.stat().st_size <= 512 * 1024
            ):
                rclone_conf_content = rc_path.read_text(
                    encoding="utf-8", errors="replace"
                )
        except OSError:
            rclone_conf_content = ""

    payload = {
        "schema": BOOTSTRAP_SCHEMA,
        "app_version": __version__,
        "exported_at": time.time(),
        "revision": str(revision or ""),
        "include_secrets": bool(include_secrets),
        "config": _sanitized_config_for_bootstrap(
            config_snapshot, include_secrets=include_secrets
        ),
        "filter_file_content": filter_content,
        "rclone_conf": rclone_conf_content,
        "data_dir_hint": (
            str(paths.get("data_dir") or "/opt/rclone-sync/data")
            if isinstance(paths, Mapping)
            else "/opt/rclone-sync/data"
        ),
    }
    plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(clean_pass, salt, KDF_ITERATIONS)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, BOOTSTRAP_AAD)
    return {
        "schema": BOOTSTRAP_SCHEMA,
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": KDF_ITERATIONS,
        "cipher": "AES-256-GCM",
        "exported_at": payload["exported_at"],
        "revision": payload["revision"],
        "include_secrets": bool(include_secrets),
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "sha256": hashlib.sha256(ciphertext).hexdigest(),
    }


def decrypt_bootstrap_bundle(
    envelope: Mapping[str, Any], *, passphrase: str
) -> dict[str, Any]:
    """Entschlüsselt und prüft ein Bootstrap-Paket."""
    if not isinstance(envelope, Mapping) or envelope.get("schema") != BOOTSTRAP_SCHEMA:
        raise BootstrapExportError("Ungültiges Bootstrap-Paket-Schema")
    clean_pass = str(passphrase or "").strip()
    if not clean_pass:
        raise BootstrapExportError("Passphrase fehlt")
    try:
        iterations = int(envelope.get("iterations") or KDF_ITERATIONS)
        if iterations < 100_000 or iterations > 5_000_000:
            raise BootstrapExportError("Ungültige KDF-Iterationen im Paket")
        salt = base64.b64decode(str(envelope.get("salt") or ""), validate=True)
        nonce = base64.b64decode(str(envelope.get("nonce") or ""), validate=True)
        ciphertext = base64.b64decode(
            str(envelope.get("ciphertext") or ""), validate=True
        )
    except (ValueError, TypeError) as exc:
        raise BootstrapExportError("Paket-Kodierung ist beschädigt") from exc
    if len(salt) != 16 or len(nonce) != 12 or not ciphertext:
        raise BootstrapExportError("Kryptografische Parameter des Pakets sind ungültig")
    expected_sha = str(envelope.get("sha256") or "").strip().lower()
    if expected_sha and not secrets.compare_digest(
        expected_sha, hashlib.sha256(ciphertext).hexdigest()
    ):
        raise BootstrapExportError(
            "Prüfsumme des Bootstrap-Pakets stimmt nicht überein"
        )
    key = _derive_key(clean_pass, salt, iterations)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, BOOTSTRAP_AAD)
    except InvalidTag as exc:
        raise BootstrapExportError(
            "Entschlüsselung fehlgeschlagen: Passphrase falsch oder Paket manipuliert"
        ) from exc
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BootstrapExportError("Entschlüsselter Paketinhalt ist ungültig") from exc
    if not isinstance(decoded, dict) or decoded.get("schema") != BOOTSTRAP_SCHEMA:
        raise BootstrapExportError("Entschlüsselter Paketinhalt hat falsches Schema")
    return decoded


def write_automatic_bootstrap_export(
    config_snapshot: Mapping[str, Any], revision: str = ""
) -> dict[str, Any] | None:
    """Schreibt bei aktivierter Option automatisch ein verschlüsseltes Bootstrap-Paket."""
    maintenance = (
        config_snapshot.get("maintenance")
        if isinstance(config_snapshot, Mapping)
        else None
    )
    boot_cfg = (
        maintenance.get("bootstrap_export")
        if isinstance(maintenance, Mapping)
        else None
    )
    if not isinstance(boot_cfg, Mapping) or not boot_cfg.get("enabled"):
        return None
    paths = (
        config_snapshot.get("paths") if isinstance(config_snapshot, Mapping) else None
    )
    data_dir = (
        str(paths.get("data_dir") or "/opt/rclone-sync/data")
        if isinstance(paths, Mapping)
        else "/opt/rclone-sync/data"
    )
    target_dir_raw = str(boot_cfg.get("target_dir") or "").strip()
    target_dir = (
        Path(target_dir_raw)
        if target_dir_raw
        else (Path(data_dir) / "bootstrap-exports")
    )
    web = config_snapshot.get("web") if isinstance(config_snapshot, Mapping) else {}
    secret_key = (
        str(web.get("secret_key") or "").strip() if isinstance(web, Mapping) else ""
    )
    if len(secret_key) < 16:
        return None

    try:
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target_dir.is_symlink():
            raise BootstrapExportError(
                "Bootstrap-Zielverzeichnis darf kein Symlink sein"
            )
        try:
            os.chmod(target_dir, stat.S_IRWXU)
        except OSError:
            pass
        envelope = create_encrypted_bootstrap_bundle(
            config_snapshot,
            passphrase=secret_key,
            revision=revision,
            include_secrets=False,
        )
        raw_bytes = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
        latest_path = target_dir / "sicherpfad-bootstrap-latest.encrypted.json"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rev_short = (revision or "norev")[:8]
        archive_name = f"sicherpfad-bootstrap-{stamp}-{rev_short}.encrypted.json"
        archive_path = target_dir / archive_name

        for dest in (archive_path, latest_path):
            if dest.is_symlink():
                raise BootstrapExportError(f"Zieldatei ist ein Symlink: {dest}")
            tmp_path = dest.with_suffix(".tmp")
            flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp_path, flags, stat.S_IRUSR | stat.S_IWUSR)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, dest)
            finally:
                tmp_path.unlink(missing_ok=True)

        # Keep latest 10 timestamped exports
        archives = sorted(
            (
                p
                for p in target_dir.glob("sicherpfad-bootstrap-*-*.encrypted.json")
                if p.is_file() and not p.is_symlink()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_file in archives[10:]:
            old_file.unlink(missing_ok=True)

        return {
            "ok": True,
            "path": str(latest_path),
            "archive": archive_name,
            "size": len(raw_bytes),
            "sha256": envelope["sha256"],
        }
    except Exception as exc:
        logger.warning("Automatischer Bootstrap-Export fehlgeschlagen: %s", exc)
        return None
