"""Content-addressed uploads from the native iPhone app.

Uploads arrive in small resumable chunks, are verified locally, copied to the
selected backup target and read back before they become restorable.  The blob
store deliberately stays separate from configured live sources: importing a
phone file must never mutate the source of an existing sync pair.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping

from .db import Database
from .notifications import notify
from .rclone_args import rclone_subprocess_env

MAX_FILE_BYTES = 50 * 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_LIBRARY_ITEMS = 500
_LOCK = threading.RLock()
_TRANSFER_LOCKS: dict[str, threading.Lock] = {}
_UPLOAD_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class VaultError(ValueError):
    """A safe error that may be shown to an authenticated client."""


def vault_root(config: Mapping[str, Any]) -> Path:
    paths = config.get("paths") if isinstance(config.get("paths"), Mapping) else {}
    data_dir = Path(str((paths or {}).get("data_dir") or "/opt/rclone-sync/data"))
    raw = str((paths or {}).get("device_vault_dir") or data_dir / "device-vault")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise VaultError("paths.device_vault_dir muss absolut sein")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("uploads", "records", "blobs"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root.resolve()


def _safe_upload_id(value: str) -> str:
    clean = str(value or "").lower()
    if (
        not clean
        or len(clean) > 80
        or any(char not in _UPLOAD_ID_CHARS for char in clean)
    ):
        raise VaultError("Ungültige Upload-ID")
    return clean


def safe_filename(value: str) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 240
        or name in {".", ".."}
        or name.startswith(".")
        or any(char in name for char in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise VaultError("Ungültiger Dateiname")
    return name


def safe_device_name(value: str) -> str:
    raw = " ".join(str(value or "iPhone").strip().split())[:80] or "iPhone"
    clean = "".join(
        char if char.isalnum() or char in {" ", "-", "_"} else "-" for char in raw
    ).strip(" .-_")
    return clean or "iPhone"


def _record_path(root: Path, upload_id: str) -> Path:
    return root / "records" / f"{_safe_upload_id(upload_id)}.json"


def _part_path(root: Path, upload_id: str) -> Path:
    return root / "uploads" / f"{_safe_upload_id(upload_id)}.part"


def _blob_path(root: Path, sha256: str) -> Path:
    return root / "blobs" / sha256[:2] / sha256


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_record(root: Path, upload_id: str) -> dict[str, Any]:
    path = _record_path(root, upload_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VaultError("Upload nicht gefunden") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise VaultError("Upload-Status ist beschädigt") from exc
    if not isinstance(value, dict) or value.get("id") != upload_id:
        raise VaultError("Upload-Status ist beschädigt")
    return value


def _save_record(root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(record)
    clean["updated_at"] = time.time()
    _atomic_json(_record_path(root, str(clean["id"])), clean)
    return clean


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _join_target(root: str, relative: str) -> str:
    if not _is_remote(root):
        return str(Path(root).joinpath(*PurePosixPath(relative).parts))
    separator = "" if root.endswith(("/", ":")) else "/"
    return f"{root}{separator}{relative}"


def _is_remote(path: str) -> bool:
    if len(path) >= 3 and path[1] == ":" and path[2] in {"/", "\\"}:
        return False
    prefix = path.split("/", 1)[0]
    return ":" in prefix and not path.startswith("/")


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "pair",
            "identity",
            "filename",
            "source_type",
            "device_name",
            "size",
            "sha256",
            "received",
            "status",
            "deduplicated",
            "verified",
            "target_relative",
            "created_at",
            "updated_at",
            "completed_at",
            "error",
        )
    }


def create_upload(
    config: Mapping[str, Any],
    *,
    pair: Mapping[str, Any],
    filename: str,
    size: int,
    sha256: str,
    source_type: str,
    device_name: str,
    target_root: str,
) -> dict[str, Any]:
    if size < 1 or size > MAX_FILE_BYTES:
        raise VaultError("Dateigröße liegt außerhalb des erlaubten Bereichs")
    digest = str(sha256 or "").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise VaultError("Ungültige SHA-256-Prüfsumme")
    if source_type not in {"photo", "file"}:
        raise VaultError("Unbekannter Quelltyp")
    if not target_root:
        raise VaultError("Der Datenweg besitzt kein Sicherungsziel")

    root = vault_root(config)
    upload_id = str(uuid.uuid4())
    blob = _blob_path(root, digest)
    deduplicated = blob.is_file() and blob.stat().st_size == size
    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    target_name = f"{stamp}_{digest[:10]}_{safe_filename(filename)}"
    target_relative = "/".join(
        (
            "Sicherpfad",
            safe_device_name(device_name),
            "Fotos" if source_type == "photo" else "Dateien",
            datetime.now(timezone.utc).strftime("%Y/%m"),
            target_name,
        )
    )
    record = {
        "id": upload_id,
        "identity": str(pair.get("id") or pair.get("name") or ""),
        "pair": str(pair.get("name") or ""),
        "filename": safe_filename(filename),
        "source_type": source_type,
        "device_name": safe_device_name(device_name),
        "size": int(size),
        "sha256": digest,
        "received": int(size if deduplicated else 0),
        "status": "uploaded" if deduplicated else "receiving",
        "deduplicated": deduplicated,
        "verified": False,
        "target_root": str(target_root),
        "target_relative": target_relative,
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    with _LOCK:
        _save_record(root, record)
        if not deduplicated:
            part = _part_path(root, upload_id)
            part.touch(mode=0o600, exist_ok=False)
    return _public_record(record)


def append_chunk(
    config: Mapping[str, Any], upload_id: str, *, offset: int, payload: bytes
) -> dict[str, Any]:
    if not payload or len(payload) > MAX_CHUNK_BYTES:
        raise VaultError(
            f"Ein Upload-Block darf höchstens {MAX_CHUNK_BYTES} Bytes enthalten"
        )
    root = vault_root(config)
    with _LOCK:
        record = _load_record(root, upload_id)
        if record.get("status") != "receiving":
            raise VaultError("Dieser Upload nimmt keine weiteren Daten an")
        part = _part_path(root, upload_id)
        current = part.stat().st_size if part.exists() else 0
        if int(offset) != current:
            raise VaultError(
                f"Upload-Versatz stimmt nicht; erwartet werden {current} Bytes"
            )
        expected = int(record.get("size") or 0)
        if current + len(payload) > expected:
            raise VaultError("Upload würde die angekündigte Dateigröße überschreiten")
        with part.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        record["received"] = current + len(payload)
        if record["received"] == expected:
            record["status"] = "uploaded"
        record = _save_record(root, record)
    return _public_record(record)


def queue_completion(config: Mapping[str, Any], upload_id: str) -> dict[str, Any]:
    root = vault_root(config)
    with _LOCK:
        record = _load_record(root, upload_id)
        if record.get("status") in {"queued", "transferring", "ready"}:
            return _public_record(record)
        if record.get("status") != "uploaded":
            raise VaultError("Upload ist noch nicht vollständig")
        record["status"] = "queued"
        record["error"] = None
        record = _save_record(root, record)
    return _public_record(record)


def _prepare_blob(root: Path, record: dict[str, Any]) -> Path:
    digest = str(record["sha256"])
    blob = _blob_path(root, digest)
    if blob.is_file():
        if blob.stat().st_size != int(record["size"]) or _hash_file(blob) != digest:
            raise RuntimeError("Vorhandener deduplizierter Blob ist beschädigt")
        _part_path(root, str(record["id"])).unlink(missing_ok=True)
        record["deduplicated"] = True
        return blob
    part = _part_path(root, str(record["id"]))
    if not part.is_file() or part.stat().st_size != int(record["size"]):
        raise RuntimeError("Lokale Upload-Datei ist unvollständig")
    if _hash_file(part) != digest:
        raise RuntimeError("SHA-256-Prüfung des Uploads ist fehlgeschlagen")
    blob.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.replace(part, blob)
    try:
        blob.chmod(0o600)
    except OSError:
        pass
    return blob


def _copy_and_verify_local(blob: Path, target: str, expected_sha256: str) -> None:
    destination = Path(target).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(blob, temporary)
        with temporary.open("rb+") as handle:
            os.fsync(handle.fileno())
        if _hash_file(temporary) != expected_sha256:
            raise RuntimeError("Prüfsumme der Zielkopie stimmt nicht")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_and_verify_remote(
    blob: Path, target: str, expected_sha256: str, *, timeout: int
) -> None:
    copied = subprocess.run(
        ["rclone", "copyto", "--immutable", "--", str(blob), target],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    copy_error = (copied.stderr or copied.stdout or "").strip()[:500]
    process = subprocess.Popen(
        ["rclone", "cat", "--", target],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    watchdog = threading.Timer(timeout, kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert process.stdout is not None
        actual = _hash_stream(process.stdout)
        _stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError(
            "Prüfung der Zielkopie hat das Zeitlimit überschritten"
        ) from None
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        raise RuntimeError("Prüfung der Zielkopie hat das Zeitlimit überschritten")
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()[:500]
        if copied.returncode != 0:
            raise RuntimeError(
                "Übertragung zum Datenweg fehlgeschlagen: "
                f"{copy_error or copied.returncode}"
            )
        raise RuntimeError(
            f"Zielkopie konnte nicht geprüft werden: {detail or process.returncode}"
        )
    if actual != expected_sha256:
        raise RuntimeError("SHA-256-Prüfung der Zielkopie ist fehlgeschlagen")


def complete_upload(
    database: Database, config: Mapping[str, Any], upload_id: str
) -> dict[str, Any]:
    root = vault_root(config)
    with _LOCK:
        transfer_lock = _TRANSFER_LOCKS.setdefault(upload_id, threading.Lock())
    if not transfer_lock.acquire(blocking=False):
        return _public_record(_load_record(root, upload_id))
    try:
        return _complete_upload_locked(database, config, upload_id, root)
    finally:
        transfer_lock.release()


def _complete_upload_locked(
    database: Database,
    config: Mapping[str, Any],
    upload_id: str,
    root: Path,
) -> dict[str, Any]:
    with _LOCK:
        record = _load_record(root, upload_id)
        if record.get("status") == "ready":
            return _public_record(record)
        if record.get("status") not in {"queued", "transferring"}:
            return _public_record(record)
        record["status"] = "transferring"
        record = _save_record(root, record)
    try:
        blob = _prepare_blob(root, record)
        target = _join_target(
            str(record["target_root"]), str(record["target_relative"])
        )
        timeout = max(
            300,
            int(float((config.get("backup") or {}).get("timeout_hours", 4)) * 3600),
        )
        if _is_remote(target):
            _copy_and_verify_remote(
                blob, target, str(record["sha256"]), timeout=timeout
            )
        else:
            _copy_and_verify_local(blob, target, str(record["sha256"]))
        with _LOCK:
            record["status"] = "ready"
            record["verified"] = True
            record["received"] = int(record["size"])
            record["completed_at"] = time.time()
            record["error"] = None
            record = _save_record(root, record)
        database.audit_add(
            "device_vault_ready",
            actor="ios",
            details={
                "id": upload_id,
                "pair": record.get("pair"),
                "filename": record.get("filename"),
                "size": record.get("size"),
                "sha256": record.get("sha256"),
                "deduplicated": record.get("deduplicated"),
            },
        )
        notify(
            "recovery_ready",
            f"{record.get('pair')}: Geräte-Datei verifiziert",
            f"{record.get('filename')} liegt geprüft im Geräte-Vault.",
            pair=record.get("pair"),
            vault_id=upload_id,
        )
    except Exception as exc:
        with _LOCK:
            record["status"] = "error"
            record["verified"] = False
            record["error"] = str(exc)[:1000]
            record = _save_record(root, record)
        database.audit_add(
            "device_vault_error",
            actor="ios",
            details={
                "id": upload_id,
                "pair": record.get("pair"),
                "error": record["error"],
            },
        )
        notify(
            "recovery_error",
            f"{record.get('pair')}: Geräte-Backup fehlgeschlagen",
            str(record["error"]),
            pair=record.get("pair"),
            vault_id=upload_id,
        )
    return _public_record(record)


def upload_status(config: Mapping[str, Any], upload_id: str) -> dict[str, Any]:
    return _public_record(_load_record(vault_root(config), upload_id))


def library(
    config: Mapping[str, Any], *, identity: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    root = vault_root(config)
    records: list[dict[str, Any]] = []
    for path in (root / "records").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if (
            identity
            and str(record.get("identity") or "") != identity
            and str(record.get("pair") or "") != identity
        ):
            continue
        records.append(_public_record(record))
    records.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return records[: max(1, min(int(limit), MAX_LIBRARY_ITEMS))]


def _restore_missing_blob(
    root: Path, config: Mapping[str, Any], record: dict[str, Any]
) -> Path:
    from .jobs.restore_test import _endpoints
    from .handover_rescue import HandoverError, _safe_relative

    # A receipt never authorizes an arbitrary path or an old/reconfigured remote.
    pairs = (config.get("backup") or {}).get("pairs") or []
    configured = next(
        (
            pair
            for pair in pairs
            if isinstance(pair, Mapping)
            and str(pair.get("id") or pair.get("name") or "")
            == str(record.get("identity") or "")
        ),
        None,
    )
    if not configured or _endpoints(configured)[1] != str(
        record.get("target_root") or ""
    ):
        raise VaultError(
            "Sicherungsziel hat sich geändert; Notfallakte einem bestehenden Datenweg neu zuordnen"
        )
    try:
        relative = _safe_relative(str(record.get("target_relative") or ""))
    except HandoverError as exc:
        raise VaultError(str(exc)) from exc
    size = int(record.get("size") or 0)
    digest = str(record.get("sha256") or "")
    if (
        not 1 <= size <= MAX_FILE_BYTES
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise VaultError("Gespeicherter Wiederherstellungsbeleg ist beschädigt")
    if shutil.disk_usage(root).free < size + 64 * 1024 * 1024:
        raise VaultError("Nicht genügend Speicher für die Wiederherstellung")
    target = _join_target(str(record["target_root"]), relative)
    temporary = root / "uploads" / f"rescue-{uuid.uuid4().hex}.part"
    process = None
    watchdog = None
    timed_out = threading.Event()
    try:
        with tempfile.TemporaryFile(dir=root / "uploads") as errors:
            if _is_remote(target):
                process = subprocess.Popen(
                    ["rclone", "cat", "--", target],
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    stdin=subprocess.DEVNULL,
                    env=rclone_subprocess_env(),
                )

                def kill_on_timeout() -> None:
                    timed_out.set()
                    process.kill()

                watchdog = threading.Timer(900, kill_on_timeout)
                watchdog.daemon = True
                watchdog.start()
                stream = process.stdout
            else:
                base = Path(str(record["target_root"])).expanduser().resolve()
                origin = Path(target).expanduser()
                if origin.is_symlink() or not origin.resolve().is_relative_to(base):
                    raise VaultError(
                        "Wiederherstellungsdatei liegt außerhalb des Sicherungsziels"
                    )
                stream = origin.open("rb")
            assert stream is not None
            actual = hashlib.sha256()
            received = 0
            with stream, temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                while True:
                    block = stream.read(min(MAX_CHUNK_BYTES, size - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > size:
                        raise VaultError(
                            "Cloud-Datei ist größer als der gespeicherte Wiederherstellungsbeleg"
                        )
                    actual.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if process is not None:
                process.wait(timeout=10)
                if process.returncode or timed_out.is_set():
                    raise VaultError(
                        "Cloud-Datei konnte nicht vollständig zurückgeholt werden; Zugang und Verbindung prüfen"
                    )
            if received != size or actual.hexdigest() != digest:
                raise VaultError(
                    "SHA-256 oder Größe der zurückgeholten Datei stimmt nicht mit dem Beleg überein"
                )
        blob = _blob_path(root, digest)
        blob.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(temporary, blob)
        record.update(
            status="ready",
            verified=True,
            received=size,
            error=None,
            completed_at=time.time(),
            rescue_verified_at=time.time(),
        )
        _save_record(root, record)
        return blob
    except (OSError, subprocess.SubprocessError) as exc:
        raise VaultError(
            "Datei konnte nicht aus dem Sicherungsziel zurückgeholt werden"
        ) from exc
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        temporary.unlink(missing_ok=True)


def download_blob(config: Mapping[str, Any], upload_id: str) -> tuple[Path, str]:
    root = vault_root(config)
    record = _load_record(root, upload_id)
    remote_receipt = record.get("status") == "remote" and bool(
        record.get("rescue_signature")
    )
    if not remote_receipt and (
        record.get("status") != "ready" or not record.get("verified")
    ):
        raise VaultError("Diese Datei ist noch nicht verifiziert und wiederherstellbar")
    blob = _blob_path(root, str(record.get("sha256") or ""))
    if not blob.is_file() or remote_receipt:
        with _LOCK:
            transfer_lock = _TRANSFER_LOCKS.setdefault(upload_id, threading.Lock())
        if not transfer_lock.acquire(blocking=False):
            raise VaultError("Diese Datei wird bereits zurückgeholt")
        try:
            blob = _restore_missing_blob(root, config, record)
        finally:
            transfer_lock.release()
    if blob.stat().st_size != int(record.get("size") or -1):
        raise VaultError("Der lokale Wiederherstellungs-Blob ist beschädigt")
    if _hash_file(blob) != str(record.get("sha256") or ""):
        raise VaultError("Der lokale Wiederherstellungs-Blob ist beschädigt")
    return blob, safe_filename(str(record.get("filename") or "Wiederherstellung"))


__all__ = [
    "MAX_CHUNK_BYTES",
    "MAX_FILE_BYTES",
    "VaultError",
    "append_chunk",
    "complete_upload",
    "create_upload",
    "download_blob",
    "library",
    "queue_completion",
    "safe_filename",
    "upload_status",
    "vault_root",
]
