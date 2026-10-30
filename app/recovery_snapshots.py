"""Complete, immutable recovery points, with bounded capture and SHA-256 evidence.

Unlike rclone backup-dir archives, each published point owns every byte and every
empty directory in the captured target. Points stay on this server; the UI must
not describe them as off-site copies or atomic filesystem snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .device_vault import _atomic_json, _is_remote
from .jobs.recovery_lifecycle import (
    RecoveryBusy,
    RecoveryCancelled,
    check_cancelled,
    ensure_cleanup_safe,
    finish_job,
    hash_file as _hash_file,
    run_command,
)
from .jobs.restore_test import _endpoints
from .jobs.restore_workspace import create_workspace, forget_workspace
from .recovery_paths import recovery_path
from .recovery_walk import WalkLimitError, bounded_walk

MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_BYTES = 51_200 * 1024 * 1024
POINT_RE = re.compile(r"full-[0-9a-f]{32}\Z")


class SnapshotError(ValueError):
    pass


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 1024
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise SnapshotError("Unsicherer Pfad im vollständigen Stand")
    return path.as_posix()


def pair_fingerprint(pair: Mapping[str, Any]) -> str:
    source, target = _endpoints(pair)
    value = [
        str(pair.get("id") or pair.get("name") or ""),
        source,
        target,
        str(pair.get("direction") or ""),
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def snapshot_root(config: Mapping[str, Any], *, create: bool = False) -> Path:
    try:
        root = recovery_path(
            config, "recovery_snapshots_dir", "recovery-snapshots"
        ).resolve()
    except ValueError as exc:
        raise SnapshotError("Recovery-Ablage muss absolut sein") from exc
    # A sync may otherwise copy/delete its own recovery store recursively.
    for pair in (config.get("backup") or {}).get("pairs") or []:
        if not isinstance(pair, Mapping):
            continue
        for endpoint in _endpoints(pair):
            if not endpoint or _is_remote(endpoint):
                continue
            live = Path(endpoint).expanduser().resolve()
            if root == live or root.is_relative_to(live) or live.is_relative_to(root):
                raise SnapshotError(
                    "Recovery-Ablage muss außerhalb aller Datenwege liegen"
                )
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _hash_inventory_file(
    path: Path, expected: os.stat_result, *, cancellable: bool = False
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            raise SnapshotError("Sicherungsdatei wurde während der Erfassung verändert")
        digest = hashlib.sha256()
        remaining = expected.st_size
        while remaining:
            if cancellable:
                check_cancelled()
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SnapshotError(
                    "Sicherungsdatei wurde während der Erfassung verkürzt"
                )
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
        if handle.read(1) or (
            after.st_size != expected.st_size
            or after.st_mtime_ns != expected.st_mtime_ns
        ):
            raise SnapshotError("Sicherungsdatei wurde während der Erfassung verändert")
        return digest.hexdigest()


def _copy_verified_file(
    origin: Path, destination: Path, expected: Mapping[str, Any]
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(origin, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != expected["size"]
            or ("modified" in expected and opened.st_mtime_ns != expected["modified"])
        ):
            raise SnapshotError("Sicherungsdatei wurde während der Erfassung verändert")
        remaining = expected["size"]
        digest = hashlib.sha256()
        with destination.open("xb") as target:
            while remaining:
                check_cancelled()
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise SnapshotError(
                        "Sicherungsdatei wurde während der Erfassung verkürzt"
                    )
                target.write(block)
                digest.update(block)
                remaining -= len(block)
            extra = source.read(1)
            after = os.fstat(source.fileno())
            if extra or (
                after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                raise SnapshotError(
                    "Sicherungsdatei wurde während der Erfassung verändert"
                )
            if digest.hexdigest() != expected["sha256"]:
                raise SnapshotError(
                    "SHA-256 des wiederhergestellten Stands stimmt nicht"
                )


def _inventory_local(
    root: Path, *, limit_bytes: int, hashes: bool = False, cancellable: bool = False
) -> tuple[dict, list]:
    files: dict[str, dict[str, Any]] = {}
    directories: list[str] = []
    total = 0
    if not root.is_dir() or root.is_symlink():
        raise SnapshotError("Sicherungsziel ist kein lesbarer regulärer Ordner")

    def fail(exc: OSError) -> None:
        raise SnapshotError(
            "Sicherungsziel konnte nicht vollständig gelesen werden"
        ) from exc

    def bounded_entries():
        try:
            yield from bounded_walk(root, max_entries=MAX_FILES, onerror=fail)
        except WalkLimitError as exc:
            raise SnapshotError(
                "Vollständiger Stand überschreitet das Datei- oder Größenlimit"
            ) from exc

    for base, dirs, names in bounded_entries():
        if cancellable:
            check_cancelled()
        for name in sorted(dirs + names):
            if cancellable:
                check_cancelled()
            item = Path(base) / name
            metadata = item.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise SnapshotError(
                    "Symbolische Links werden im vollständigen Stand nicht unterstützt"
                )
            relative = _relative(item.relative_to(root).as_posix())
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
                if len(files) + len(directories) >= MAX_FILES or total > limit_bytes:
                    raise SnapshotError(
                        "Vollständiger Stand überschreitet das Datei- oder Größenlimit"
                    )
                files[relative] = {
                    "size": metadata.st_size,
                    "modified": metadata.st_mtime_ns,
                }
                if hashes:
                    files[relative]["sha256"] = _hash_inventory_file(
                        item, metadata, cancellable=cancellable
                    )
            else:
                raise SnapshotError(
                    "Spezialdateien werden im vollständigen Stand nicht unterstützt"
                )
            if len(files) + len(directories) > MAX_FILES or total > limit_bytes:
                raise SnapshotError(
                    "Vollständiger Stand überschreitet das Datei- oder Größenlimit"
                )
    return files, sorted(directories)


def _run(command: list[str], *, timeout: int = 900) -> None:
    result = run_command(command, timeout=timeout)
    if result.returncode:
        raise SnapshotError(
            f"Recovery-Übertragung oder Zielprüfung fehlgeschlagen (exit {result.returncode})"
        )


def _inventory_remote(
    target: str, work: Path, limit_bytes: int, *, cancellable: bool = False
) -> tuple[dict, list]:
    try:
        result = run_command(
            ["rclone", "lsjson", "--recursive", "--", target],
            timeout=120,
            max_output_bytes=MAX_MANIFEST_BYTES,
            cancellable=cancellable,
        )
    except RecoveryCancelled:
        raise
    except RuntimeError as exc:
        raise SnapshotError(str(exc)) from exc
    if result.returncode:
        raise SnapshotError("Sicherungsbestand ist nicht vollständig lesbar")
    try:
        rows = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SnapshotError("Sicherungsziel lieferte ungültigen Bestand") from exc
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        raise SnapshotError("Sicherungsbestand überschreitet das Dateilimit")
    files, directories, total = {}, set(), 0
    for row in rows:
        if cancellable:
            check_cancelled()
        if not isinstance(row, dict):
            raise SnapshotError("Ungültiger Eintrag im Sicherungsbestand")
        relative = _relative(str(row.get("Path") or ""))
        if relative in files or relative in directories:
            raise SnapshotError("Doppelte Pfade im Sicherungsbestand")
        if row.get("IsDir"):
            directories.add(relative)
            continue
        size = row.get("Size")
        if not isinstance(size, int) or size < 0:
            raise SnapshotError("Ungültige Dateigröße im Sicherungsbestand")
        total += size
        if total > limit_bytes:
            raise SnapshotError("Vollständiger Stand überschreitet das Größenlimit")
        files[relative] = {"size": size, "modified": str(row.get("ModTime") or "")}
    return files, sorted(directories)


def capture_snapshot(
    config: Mapping[str, Any], pair: Mapping[str, Any], *, max_total_mb: int = 5120
) -> dict:
    limit = max(1, min(int(max_total_mb), 51_200)) * 1024 * 1024
    bound_config = dict(config)
    bound_config["backup"] = {
        **(config.get("backup") or {}),
        "pairs": [*((config.get("backup") or {}).get("pairs") or []), pair],
    }
    root = snapshot_root(bound_config, create=True)
    point_id = f"full-{uuid.uuid4().hex}"
    work, workspace_lease = create_workspace(
        root, prefix="restore-snapshot-capture-", kind="snapshot-capture"
    )
    source, target = _endpoints(pair)
    timeout = max(
        300, int(float((config.get("backup") or {}).get("timeout_hours", 4)) * 3600)
    )
    try:
        _atomic_json(
            work / ".capture-owner.json",
            {"schema": "sicherpfad-capture-draft-v1", "point_id": point_id},
        )
        before, directories = (
            _inventory_remote(target, work, limit, cancellable=True)
            if _is_remote(target)
            else _inventory_local(
                Path(target).expanduser().resolve(),
                limit_bytes=limit,
                hashes=True,
                cancellable=True,
            )
        )
        required = sum(item["size"] for item in before.values())
        if shutil.disk_usage(root).free < required + 64 * 1024 * 1024:
            raise SnapshotError(
                "Nicht genügend freier Speicher für den vollständigen Stand"
            )
        data = work / "data"
        data.mkdir(mode=0o700)
        if _is_remote(target):
            _run(
                [
                    "rclone",
                    "copy",
                    "--create-empty-src-dirs",
                    "--max-transfer",
                    str(limit),
                    "--cutoff-mode",
                    "HARD",
                    "--",
                    target,
                    str(data),
                ],
                timeout=timeout,
            )
        else:
            target_root = Path(target).expanduser().resolve()
            for relative in directories:
                check_cancelled()
                (data / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
            for relative, info in before.items():
                check_cancelled()
                origin = target_root / relative
                destination = data / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if origin.is_symlink() or not origin.resolve().is_relative_to(
                    target_root
                ):
                    raise SnapshotError(
                        "Sicherungsziel wurde während der Erfassung verändert"
                    )
                _copy_verified_file(origin, destination, info)
        captured, captured_dirs = _inventory_local(
            data, limit_bytes=limit, hashes=True, cancellable=True
        )
        if set(captured) != set(before) or any(
            captured[p]["size"] != before[p]["size"] for p in before
        ):
            raise SnapshotError(
                "Vollständiger Stand enthält nicht alle erwarteten Dateien"
            )
        after, after_dirs = (
            _inventory_remote(target, work, limit, cancellable=True)
            if _is_remote(target)
            else _inventory_local(
                Path(target).expanduser().resolve(),
                limit_bytes=limit,
                hashes=True,
                cancellable=True,
            )
        )
        if before != after or directories != after_dirs:
            raise SnapshotError(
                "Sicherungsbestand änderte sich während der Erfassung; erneut versuchen"
            )
        # Remote providers need not expose SHA-256; download-compare every captured file.
        if _is_remote(target) and captured:
            _run(
                ["rclone", "check", "--download", "--", str(data), target],
                timeout=timeout,
            )
        if not set(directories).issubset(captured_dirs):
            raise SnapshotError(
                "Vollständiger Stand enthält nicht alle erwarteten Ordner"
            )
        manifest = {
            "schema": "sicherpfad-full-snapshot-v1",
            "id": point_id,
            "identity": str(pair.get("id") or pair.get("name") or ""),
            "pair": str(pair.get("name") or ""),
            "fingerprint": pair_fingerprint(pair),
            "created_at": time.time(),
            "kind": "full",
            "complete": True,
            "total_bytes": required,
            "files": len(captured),
            "storage": "server",
            "verification": "all-files-sha256",
            "directories": captured_dirs,
            "entries": [
                {"path": p, "size": item["size"], "sha256": item["sha256"]}
                for p, item in sorted(captured.items())
            ],
        }
        if (
            len(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            > MAX_MANIFEST_BYTES
        ):
            raise SnapshotError("Vollständiger Stand überschreitet das Manifestlimit")
        check_cancelled()
        _atomic_json(work / "manifest.json", manifest)
        (work / ".capture-owner.json").unlink()
        check_cancelled()
        os.replace(work, root / point_id)
        prune_snapshots(config, pair, keep_id=point_id)
        return public_snapshot(manifest)
    finally:
        try:
            if work.exists() and work.parent == root:
                ensure_cleanup_safe()
                shutil.rmtree(work)
            forget_workspace(work)
        except RecoveryBusy:
            # The registry must survive until startup confirms that every
            # registered child has exited. The workspace is still unverified.
            pass
        finally:
            workspace_lease.release()


def capture_if_enabled(
    config: Mapping[str, Any], pair: Mapping[str, Any]
) -> dict | None:
    if pair.get("recovery_snapshots") is not True:
        return None
    return capture_snapshot(
        config, pair, max_total_mb=int(pair.get("recovery_snapshot_max_mb") or 5120)
    )


def public_snapshot(manifest: Mapping[str, Any]) -> dict:
    return {
        key: manifest.get(key)
        for key in (
            "id",
            "identity",
            "pair",
            "created_at",
            "kind",
            "complete",
            "total_bytes",
            "files",
            "storage",
            "verification",
            "historical_binding",
            "current_restore_proof",
        )
    }


def prune_snapshots(
    config: Mapping[str, Any], pair: Mapping[str, Any], *, keep_id: str
) -> None:
    """Age retention only for validated points of this data path; keep newest."""
    days = int((config.get("backup") or {}).get("recovery_snapshot_retention_days", 90))
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    root = snapshot_root(config)
    for folder in root.iterdir():
        if (
            folder.name == keep_id
            or folder.is_symlink()
            or not POINT_RE.fullmatch(folder.name)
        ):
            continue
        try:
            manifest = load_manifest(
                config, pair, folder.name, allow_previous_binding=True
            )
        except SnapshotError:
            continue
        if float(manifest["created_at"]) < cutoff:
            shutil.rmtree(folder)


def reconcile_snapshot_captures(config: Mapping[str, Any]) -> None:
    """Called only under the free recovery/sync scope lock at startup."""
    ensure_cleanup_safe()
    root = snapshot_root(config)
    if not root.is_dir():
        return
    for folder in root.iterdir():
        marker = folder / ".capture-owner.json"
        if (
            not folder.name.startswith(".capture-")
            or folder.is_symlink()
            or marker.is_symlink()
        ):
            continue
        try:
            if not marker.is_file() or marker.stat().st_size > 1024:
                continue
            owner = json.loads(marker.read_text(encoding="utf-8"))
            if owner.get(
                "schema"
            ) != "sicherpfad-capture-draft-v1" or not POINT_RE.fullmatch(
                str(owner.get("point_id") or "")
            ):
                continue
        except (OSError, ValueError, AttributeError):
            continue
        shutil.rmtree(folder)


def load_manifest(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    point_id: str,
    *,
    allow_previous_binding: bool = False,
) -> dict:
    if not POINT_RE.fullmatch(point_id):
        raise SnapshotError("Vollständiger Stand nicht gefunden")
    root = snapshot_root(config)
    folder = root / point_id
    path = folder / "manifest.json"
    if folder.is_symlink() or path.is_symlink():
        raise SnapshotError("Unsichere Recovery-Ablage")
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise SnapshotError("Recovery-Manifest ist zu groß")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(
            "Vollständiger Stand nicht gefunden oder beschädigt"
        ) from exc
    historical_binding = False
    if (
        isinstance(value, dict)
        and not allow_previous_binding
        and value.get("fingerprint") != pair_fingerprint(pair)
    ):
        from .migration_lineage import accepts

        try:
            historical_binding = accepts(config, pair, value)
        except (ValueError, OSError) as exc:
            raise SnapshotError(
                "Vollständiger Stand gehört nicht zu diesem Datenweg; historische Bindung ist nicht verifiziert"
            ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "sicherpfad-full-snapshot-v1"
        or value.get("id") != point_id
        or (
            not allow_previous_binding
            and value.get("fingerprint") != pair_fingerprint(pair)
            and not historical_binding
        )
        or (
            allow_previous_binding
            and value.get("identity") != str(pair.get("id") or pair.get("name") or "")
        )
        or value.get("complete") is not True
    ):
        raise SnapshotError("Vollständiger Stand gehört nicht zu diesem Datenweg")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise SnapshotError("Recovery-Manifest ist beschädigt")
    seen = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise SnapshotError("Recovery-Manifest ist beschädigt")
        relative = _relative(str(entry.get("path") or ""))
        if relative in seen or not re.fullmatch(
            r"[0-9a-f]{64}", str(entry.get("sha256") or "")
        ):
            raise SnapshotError("Recovery-Manifest ist beschädigt")
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise SnapshotError("Recovery-Manifest ist beschädigt")
        total += entry["size"]
        seen.add(relative)
    directories = value.get("directories")
    if not isinstance(directories, list) or len(directories) + len(entries) > MAX_FILES:
        raise SnapshotError("Recovery-Manifest ist beschädigt")
    for directory in directories:
        _relative(str(directory))
    if (
        total > MAX_BYTES
        or total != value.get("total_bytes")
        or len(entries) != value.get("files")
    ):
        raise SnapshotError("Recovery-Manifest ist unvollständig")
    if historical_binding:
        value = {**value, "historical_binding": True, "current_restore_proof": False}
    return value


def list_snapshots(config: Mapping[str, Any], pair: Mapping[str, Any]) -> list[dict]:
    root = snapshot_root(config)
    if not root.is_dir():
        return []
    result = []
    for directory in root.iterdir():
        if not POINT_RE.fullmatch(directory.name):
            continue
        try:
            result.append(public_snapshot(load_manifest(config, pair, directory.name)))
        except SnapshotError:
            continue
    # Retained full points must remain discoverable and removable. Retention,
    # rather than an arbitrary display cut-off, bounds their lifetime.
    return sorted(result, key=lambda row: row["created_at"], reverse=True)


def snapshot_target(
    config: Mapping[str, Any], pair: Mapping[str, Any], point_id: str
) -> str:
    load_manifest(config, pair, point_id)
    data = snapshot_root(config) / point_id / "data"
    if data.is_symlink() or not data.is_dir():
        raise SnapshotError("Vollständiger Stand ist nicht mehr verfügbar")
    return str(data)


def run_snapshot_capture(
    database, config, pair, *, max_total_mb: int, job_id: int
) -> dict:
    try:
        result = {
            "ok": True,
            "snapshot": capture_snapshot(config, pair, max_total_mb=max_total_mb),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc)[:500],
            "cancelled": isinstance(exc, RecoveryCancelled),
        }
    status = (
        "ok" if result["ok"] else ("cancelled" if result.get("cancelled") else "error")
    )
    finish_job(database, job_id, status, result)
    database.audit_add(
        "recovery_snapshot_captured", actor="web", details={"job_id": job_id, **result}
    )
    return result


def run_snapshot_restore(
    database, config, pair, *, point_id: str, max_total_mb: int, job_id: int
) -> dict:
    from .jobs.selective_restore import _save_record, staging_root, check_staging_budget

    root: Path | None = None
    work: Path | None = None
    record = {
        "id": f"recovery-{job_id}",
        "job_id": job_id,
        "pair": str(pair.get("name") or ""),
        "created_at": time.time(),
        "recovery_point": point_id,
        "status": "running",
        "verified": False,
        "verification_scope": "full-snapshot",
    }
    created = False
    try:
        root = staging_root(config, pair=pair)
        work = root / record["id"]
        manifest = load_manifest(config, pair, point_id)
        if manifest.get("historical_binding"):
            record.update(historical_binding=True, current_restore_proof=False)
        total = manifest["total_bytes"]
        if total > max(1, min(max_total_mb, 51_200)) * 1024 * 1024:
            raise SnapshotError(
                "Vollständiger Stand überschreitet das Wiederherstellungslimit"
            )
        check_staging_budget(database, config, total)
        record["bytes"] = total
        if shutil.disk_usage(root).free < total + 64 * 1024 * 1024:
            raise SnapshotError("Nicht genügend Speicher für die Wiederherstellung")
        source = Path(snapshot_target(config, pair, point_id))
        check_cancelled()
        record["staging_path"] = str(work / "data")
        _save_record(database, record)
        work.mkdir(mode=0o700)
        created = True
        data = work / "data"
        data.mkdir(mode=0o700)
        for directory in manifest["directories"]:
            check_cancelled()
            (data / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in manifest["entries"]:
            check_cancelled()
            origin = source / entry["path"]
            if origin.is_symlink() or not origin.resolve().is_relative_to(source):
                raise SnapshotError("Unsichere Datei im vollständigen Stand")
            destination = data / entry["path"]
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_verified_file(origin, destination, entry)
        actual, _dirs = _inventory_local(
            data, limit_bytes=MAX_BYTES, hashes=True, cancellable=True
        )
        expected = {
            entry["path"]: {"size": entry["size"], "sha256": entry["sha256"]}
            for entry in manifest["entries"]
        }
        if {
            path: {"size": item["size"], "sha256": item["sha256"]}
            for path, item in actual.items()
        } != expected:
            raise SnapshotError("Wiederherstellung ist unvollständig")
        check_cancelled()
        record.update(
            status="ready",
            verified=True,
            files=len(actual),
            bytes=total,
            staging_path=str(data),
            completed_at=time.time(),
            manifest_sha256=_hash_file(source.parent / "manifest.json"),
        )
    except Exception as exc:
        record.update(
            status="cancelled" if isinstance(exc, RecoveryCancelled) else "error",
            verified=False,
            error=str(exc)[:500],
        )
        if created and work is not None and work.exists() and work.parent == root:
            try:
                ensure_cleanup_safe()
                shutil.rmtree(work)
            except (OSError, RecoveryBusy) as cleanup_error:
                record.update(cleanup_required=True, cleanup_error=str(cleanup_error))
    finish_job(
        database, job_id, "ok" if record["verified"] else record["status"], record
    )
    _save_record(database, record)
    database.audit_add("full_recovery_finished", actor="web", details=record)
    return record
