"""Content-addressed uploads from the native iPhone app.

Uploads arrive in small resumable chunks, are verified locally, copied to the
selected backup target and read back before they become restorable.  The blob
store deliberately stays separate from configured live sources. Target copies
live in the sync-excluded ``Sicherpfad`` subtree of the selected data path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import unicodedata
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping

from .db import Database
from .file_lock import acquire as acquire_file_lock
from .file_lock import release as release_file_lock
from .jobs.restore_workspace import (
    cleanup_orphaned_workspaces,
    create_workspace,
    forget_workspace,
)
from .notifications import notify
from .rclone_args import rclone_subprocess_env

MAX_FILE_BYTES = 50 * 1024 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_LIBRARY_ITEMS = 500
_LOCK = threading.RLock()
_TRANSFER_LOCKS: dict[str, threading.Lock] = {}
# Bounded stripes also coordinate different upload IDs with identical content.
_BLOB_LOCKS = tuple(threading.RLock() for _ in range(64))
_TARGET_LOCKS = tuple(threading.Lock() for _ in range(64))
_UPLOAD_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class VaultError(ValueError):
    """A safe error that may be shown to an authenticated client."""


def endpoint_binding(pair: Mapping[str, Any]) -> dict[str, str]:
    """Only endpoint semantics bind an upload; scheduling changes do not."""
    return {
        "local": str(pair.get("local") or ""),
        "remote": str(pair.get("remote") or ""),
        "direction": str(pair.get("direction") or "bisync").lower().strip(),
    }


def _validate_destination(config: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    from .jobs.restore_test import _endpoints

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
    binding = record.get("endpoint_binding")
    if (
        configured is None
        or str(record.get("target_root") or "")
        not in {target_root(configured), _endpoints(configured)[1]}
        or (binding is not None and binding != endpoint_binding(configured))
    ):
        raise VaultError(
            "Datenweg hat sich geändert oder wurde entfernt; lokale Datei bleibt erhalten. "
            "Konfiguration neu laden und die Datei dem gewünschten Datenweg neu zuordnen."
        )


def vault_root(config: Mapping[str, Any]) -> Path:
    paths = config.get("paths") if isinstance(config.get("paths"), Mapping) else {}
    data_dir = Path(str((paths or {}).get("data_dir") or "/opt/rclone-sync/data"))
    raw = str((paths or {}).get("device_vault_dir") or data_dir / "device-vault")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise VaultError("paths.device_vault_dir muss absolut sein")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("uploads", "records", "blobs", "locks"):
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
    raw = str(value or "")
    if any(unicodedata.category(char) == "Cc" for char in raw):
        raise VaultError("Ungültiger Dateiname")
    name = raw.strip()
    if (
        not name
        or len(name) > 240
        or name in {".", ".."}
        or name.startswith(".")
        or any(char in name for char in ("/", "\\"))
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


@contextmanager
def _upload_record_lock(root: Path, upload_id: str) -> Iterator[None]:
    """Serialize chunk bytes and their receipt across service processes.

    Lock files must remain in place: deleting one could let a new process lock
    another inode while the original owner still writes this upload.
    """
    path = root / "locks" / f"{_safe_upload_id(upload_id)}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with _LOCK:
        fd = os.open(path, flags, 0o600)
        acquired = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise VaultError("Upload-Lock ist keine reguläre Datei")
            acquire_file_lock(fd)
            acquired = True
            yield
        finally:
            if acquired:
                release_file_lock(fd)
            os.close(fd)


def _reconcile_received(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Keep only acknowledged bytes after a crash before receipt persistence.

    The client can safely resend the discarded suffix. Never infer that
    unacknowledged bytes are valid or discard a resumable upload wholesale.
    Caller holds the upload record lock.
    """
    if record.get("status") != "receiving":
        return record
    expected = int(record.get("size") or 0)
    received = int(record.get("received") or 0)
    if not 0 <= received <= expected or expected < 1:
        raise VaultError("Upload-Status ist beschädigt")
    part = _part_path(root, str(record["id"]))
    current = part.stat().st_size if part.exists() else 0
    if current > received:
        with part.open("rb+") as handle:
            handle.truncate(received)
            handle.flush()
            os.fsync(handle.fileno())
        current = received
    changed = current != received
    record["received"] = current
    if current == expected:
        record["status"] = "uploaded"
        changed = True
    return _save_record(root, record) if changed else record


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
    with _open_regular(path) as handle:
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


def target_root(pair: Mapping[str, Any]) -> str:
    """Vault destination is explicit; bisync's remote side is preferred."""
    direction = str(pair.get("direction") or "bisync").lower().strip()
    return str((pair.get("local") if direction == "pull" else pair.get("remote")) or "")


def _guard_local_target(pair: Mapping[str, Any], target: str) -> None:
    from .jobs.rclone_sync import _check_local_endpoint

    field = "remote" if str(pair.get("remote") or "") == target else "local"
    allowed, reason = _check_local_endpoint(
        dict(pair), field, Path(target), include_counts=False, include_free_space=True
    )
    if not allowed:
        raise VaultError(reason)


def _confined_local_path(base: Path, path: Path) -> Path:
    base = base.expanduser().absolute()
    path = path.expanduser().absolute()
    if not path.is_relative_to(base) or not path.resolve().is_relative_to(
        base.resolve()
    ):
        raise VaultError("Datei liegt außerhalb des Sicherungsziels")
    for component in (base, *base.parents, *path.relative_to(base).parents):
        candidate = component if component.is_absolute() else base / component
        if candidate.is_symlink():
            raise VaultError("Symlinks im Sicherungsziel sind nicht erlaubt")
    if path.is_symlink():
        raise VaultError("Symlinks im Sicherungsziel sind nicht erlaubt")
    return path


def _open_regular(path: Path) -> BinaryIO:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise VaultError("Die Wiederherstellungsdatei ist keine reguläre Datei")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _anchored_local_parent(base: Path, path: Path, *, create: bool = False):
    """Walk from the filesystem root without following any directory symlink.

    All subsequent file operations use the retained parent descriptor. A rename
    or symlink substitution of an ancestor cannot redirect them to another tree.
    Unsupported hosts fail closed rather than reverting to pathname I/O.
    """
    needed = (os.open, os.mkdir, os.stat, os.unlink, os.rename, os.rmdir)
    if (
        os.name != "posix"
        or not all(call in os.supports_dir_fd for call in needed)
        or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        )
    ):
        raise VaultError(
            "Sichere lokale Vault-Dateizugriffe benötigen POSIX-Verzeichnisdeskriptoren"
        )
    base = base.expanduser().absolute()
    path = path.expanduser().absolute()
    if (
        ".." in base.parts
        or ".." in path.parts
        or path == base
        or not path.is_relative_to(base)
    ):
        raise VaultError("Datei liegt außerhalb des Sicherungsziels")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current = os.open(path.anchor, flags)
    try:
        for component in path.parent.parts[1:]:
            try:
                following = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o750, dir_fd=current)
                except FileExistsError:
                    pass
                following = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = following
        yield current, path.name
    except OSError as exc:
        raise VaultError(
            "Das lokale Sicherungsziel wurde verändert oder ist nicht sicher zugänglich"
        ) from exc
    finally:
        os.close(current)


def _open_local_target(base: Path, path: Path) -> BinaryIO:
    with _anchored_local_parent(base, path) as (parent, name):
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise VaultError("Die Wiederherstellungsdatei ist keine reguläre Datei")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    result = {
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
            "target_root",
            "cloud",
            "local_only",
            "endpoint_binding",
        )
    }
    result["cloud"] = _is_remote(str(record.get("target_root") or ""))
    result["local_only"] = not result["cloud"]
    return result


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
    local_only: bool = False,
    expected_endpoints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    binding = endpoint_binding(pair)
    if (
        expected_endpoints is not None
        and endpoint_binding(expected_endpoints) != binding
    ):
        raise VaultError(
            "Datenweg hat sich geändert; Konfiguration neu laden und das Sicherungsziel prüfen."
        )
    if size < 1 or size > MAX_FILE_BYTES:
        raise VaultError("Dateigröße liegt außerhalb des erlaubten Bereichs")
    digest = str(sha256 or "").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise VaultError("Ungültige SHA-256-Prüfsumme")
    if source_type not in {"photo", "file"}:
        raise VaultError("Unbekannter Quelltyp")
    if not target_root:
        raise VaultError("Der Datenweg besitzt kein Sicherungsziel")
    if not _is_remote(target_root):
        if not local_only:
            raise VaultError(
                "Dieses Ziel liegt nur lokal. Lokale Ablage ausdrücklich bestätigen."
            )
        _guard_local_target(pair, target_root)

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
        "cloud": _is_remote(target_root),
        "local_only": not _is_remote(target_root),
        "endpoint_binding": binding,
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
    with _upload_record_lock(root, upload_id):
        record = _reconcile_received(root, _load_record(root, upload_id))
        _validate_destination(config, record)
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
    with _upload_record_lock(root, upload_id):
        record = _reconcile_received(root, _load_record(root, upload_id))
        if record.get("status") != "ready":
            _validate_destination(config, record)
        if record.get("status") in {"queued", "transferring", "ready"}:
            return _public_record(record)
        if record.get("status") != "uploaded":
            raise VaultError("Upload ist noch nicht vollständig")
        record["status"] = "queued"
        record["error"] = None
        record = _save_record(root, record)
    return _public_record(record)


def _prepare_blob(root: Path, record: dict[str, Any]) -> Path:
    with _blob_lock(str(record["sha256"])):
        return _prepare_blob_locked(root, record)


def _blob_lock(digest: str):
    return _BLOB_LOCKS[int(digest[:8], 16) % len(_BLOB_LOCKS)]


def _target_lock(target: str):
    identity = (
        target if _is_remote(target) else os.path.normcase(str(Path(target).resolve()))
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _TARGET_LOCKS[int(digest[:8], 16) % len(_TARGET_LOCKS)]


def _prepare_blob_locked(root: Path, record: dict[str, Any]) -> Path:
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


def _copy_and_verify_local(
    blob: Path, target: str, expected_sha256: str, *, base: str | None = None
) -> None:
    destination = Path(target).expanduser()
    boundary = Path(base) if base else destination.parent
    with _anchored_local_parent(boundary, destination, create=True) as (parent, name):
        staging_name = f".sicherpfad-{uuid.uuid4().hex}"
        os.mkdir(staging_name, mode=0o700, dir_fd=parent)
        staging = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        temporary = "payload"
        owns_staging = False
        try:
            staging_metadata = os.fstat(staging)
            if (
                staging_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(staging_metadata.st_mode) & 0o077
            ):
                raise VaultError(
                    "Die private Ablage des Sicherungsziels wurde verändert"
                )
            owns_staging = True
            descriptor = os.open(
                temporary,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=staging,
            )
            with os.fdopen(descriptor, "w+b") as output, _open_regular(blob) as source:
                shutil.copyfileobj(source, output, length=MAX_CHUNK_BYTES)
                output.flush()
                os.fsync(output.fileno())
                output.seek(0)
                if _hash_stream(output) != expected_sha256:
                    raise RuntimeError("Prüfsumme der Zielkopie stimmt nicht")
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                metadata = None
            if metadata is not None and not stat.S_ISREG(metadata.st_mode):
                raise VaultError("Die Zieldatei ist keine reguläre Datei")
            os.rename(temporary, name, src_dir_fd=staging, dst_dir_fd=parent)
            os.fsync(parent)
            # Do not claim an accessible target copy if its directory was moved
            # or replaced while bytes were published through the retained fd.
            with _anchored_local_parent(boundary, destination) as (current, _):
                if (os.fstat(current).st_dev, os.fstat(current).st_ino) != (
                    os.fstat(parent).st_dev,
                    os.fstat(parent).st_ino,
                ):
                    raise VaultError(
                        "Das Sicherungsziel hat sich während der Ablage verändert"
                    )
        finally:
            try:
                if owns_staging:
                    os.unlink(temporary, dir_fd=staging)
            except FileNotFoundError:
                pass
            finally:
                os.close(staging)
                try:
                    if owns_staging:
                        os.rmdir(staging_name, dir_fd=parent)
                except OSError:
                    # A renamed private staging directory is empty after publish;
                    # do not try to locate or remove it through another pathname.
                    pass


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
    if copied.returncode != 0:
        raise RuntimeError(
            "Übertragung zum Datenweg fehlgeschlagen: "
            f"{copy_error or copied.returncode}"
        )
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
        raise RuntimeError(
            f"Zielkopie konnte nicht geprüft werden: {detail or process.returncode}"
        )
    if actual != expected_sha256:
        raise RuntimeError("SHA-256-Prüfung der Zielkopie ist fehlgeschlagen")


def complete_upload(
    database: Database,
    config: Mapping[str, Any],
    upload_id: str,
    *,
    config_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    root = vault_root(config)
    with _LOCK:
        transfer_lock = _TRANSFER_LOCKS.setdefault(upload_id, threading.Lock())
        if not transfer_lock.acquire(blocking=False):
            return _public_record(_load_record(root, upload_id))
    try:
        return _complete_upload_locked(
            database, config, upload_id, root, config_provider=config_provider
        )
    finally:
        with _LOCK:
            transfer_lock.release()
            if _TRANSFER_LOCKS.get(upload_id) is transfer_lock:
                _TRANSFER_LOCKS.pop(upload_id, None)


def _complete_upload_locked(
    database: Database,
    config: Mapping[str, Any],
    upload_id: str,
    root: Path,
    *,
    config_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    with _upload_record_lock(root, upload_id):
        record = _load_record(root, upload_id)
        if record.get("status") == "ready":
            return _public_record(record)
        if record.get("status") not in {"queued", "transferring"}:
            return _public_record(record)
        record["status"] = "transferring"
        record = _save_record(root, record)
    try:
        current_config = config_provider() if config_provider else config
        _validate_destination(current_config, record)
        blob = _prepare_blob(root, record)
        # Preparing/hashing a large payload can outlive a configuration edit.
        current_config = config_provider() if config_provider else config
        _validate_destination(current_config, record)
        target = _join_target(
            str(record["target_root"]), str(record["target_relative"])
        )
        timeout = max(
            300,
            int(
                float((current_config.get("backup") or {}).get("timeout_hours", 4))
                * 3600
            ),
        )
        # Identical uploads share the timestamp/hash-based destination as well
        # as the local blob. Publish and verify that destination serially.
        with _target_lock(target):
            if _is_remote(target):
                _copy_and_verify_remote(
                    blob, target, str(record["sha256"]), timeout=timeout
                )
            else:
                pair = next(
                    (
                        pair
                        for pair in (current_config.get("backup") or {}).get(
                            "pairs", []
                        )
                        if str(pair.get("id") or pair.get("name") or "")
                        == str(record.get("identity") or "")
                    ),
                    None,
                )
                if not isinstance(pair, Mapping):
                    raise VaultError(
                        "Datenweg für die lokale Ablage ist nicht mehr verfügbar"
                    )
                _guard_local_target(pair, str(record["target_root"]))
                _copy_and_verify_local(
                    blob, target, str(record["sha256"]), base=str(record["target_root"])
                )
        with _upload_record_lock(root, upload_id):
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
        with _upload_record_lock(root, upload_id):
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
    root = vault_root(config)
    with _upload_record_lock(root, upload_id):
        return _public_record(_reconcile_received(root, _load_record(root, upload_id)))


def cleanup_vault_scratch(config: Mapping[str, Any]) -> dict[str, int]:
    paths = config.get("paths") or {}
    root = Path(
        str(
            paths.get("device_vault_dir")
            or Path(str(paths.get("data_dir") or "/opt/rclone-sync/data"))
            / "device-vault"
        )
    ).expanduser()
    if not root.is_absolute():
        raise VaultError("paths.device_vault_dir muss absolut sein")
    return cleanup_orphaned_workspaces(root / "uploads")


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
    from .handover_rescue import HandoverError, _safe_relative

    # A receipt never authorizes an arbitrary path or an old/reconfigured remote.
    _validate_destination(config, record)
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
    workspace, workspace_lease = create_workspace(
        root / "uploads", "restore-vault-rescue-", kind="vault-rescue"
    )
    temporary = workspace / "data.part"
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
                base = Path(str(record["target_root"])).expanduser()
                stream = _open_local_target(base, Path(target))
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
                            "Zielkopie ist größer als der gespeicherte Wiederherstellungsbeleg"
                        )
                    actual.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if process is not None:
                process.wait(timeout=10)
                if process.returncode or timed_out.is_set():
                    raise VaultError(
                        "Zielkopie konnte nicht vollständig zurückgeholt werden; Zugang und Verbindung prüfen"
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
        try:
            if watchdog is not None:
                watchdog.cancel()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        finally:
            try:
                shutil.rmtree(workspace)
                forget_workspace(workspace)
            finally:
                workspace_lease.release()


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
            with _blob_lock(str(record["sha256"])):
                blob = _restore_missing_blob(root, config, record)
        finally:
            with _LOCK:
                transfer_lock.release()
                if _TRANSFER_LOCKS.get(upload_id) is transfer_lock:
                    _TRANSFER_LOCKS.pop(upload_id, None)
    if blob.stat().st_size != int(record.get("size") or -1):
        raise VaultError("Der lokale Wiederherstellungs-Blob ist beschädigt")
    if _hash_file(blob) != str(record.get("sha256") or ""):
        raise VaultError("Der lokale Wiederherstellungs-Blob ist beschädigt")
    return blob, safe_filename(str(record.get("filename") or "Wiederherstellung"))


def remove_local_copy(config: Mapping[str, Any], upload_id: str) -> dict[str, Any]:
    """Delete the server receipt/cache only, never touch a configured target.

    Shared deduplicated blobs remain while any other receipt references them.
    There is deliberately no automatic expiry of a potentially sole copy.
    """
    root = vault_root(config)
    with _LOCK:
        record = _load_record(root, upload_id)
        if upload_id in _TRANSFER_LOCKS or record.get("status") in {
            "queued",
            "transferring",
        }:
            raise VaultError("Diese Datei wird gerade übertragen oder zurückgeholt")
        digest = str(record.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise VaultError("Upload-Status ist beschädigt")
        with _blob_lock(digest):
            others = []
            for path in (root / "records").glob("*.json"):
                if path == _record_path(root, upload_id):
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    # An unreadable receipt might still reference this blob.
                    raise VaultError(
                        "Beschädigte Vault-Einträge vor dem Löschen prüfen"
                    ) from None
                if not isinstance(value, dict):
                    raise VaultError(
                        "Beschädigte Vault-Einträge vor dem Löschen prüfen"
                    )
                if value.get("sha256") == digest:
                    others.append(path)
            _confined_local_path(root, _part_path(root, upload_id)).unlink(
                missing_ok=True
            )
            if not others:
                _confined_local_path(root, _blob_path(root, digest)).unlink(
                    missing_ok=True
                )
            _confined_local_path(root, _record_path(root, upload_id)).unlink()
    return {"ok": True, "target_untouched": True, "shared_blob_retained": bool(others)}


__all__ = [
    "MAX_CHUNK_BYTES",
    "MAX_FILE_BYTES",
    "VaultError",
    "append_chunk",
    "complete_upload",
    "create_upload",
    "download_blob",
    "library",
    "remove_local_copy",
    "target_root",
    "queue_completion",
    "safe_filename",
    "upload_status",
    "vault_root",
]
