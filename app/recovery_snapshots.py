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
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .device_vault import _atomic_json, _hash_file, _is_remote
from .jobs.restore_test import _endpoints
from .rclone_args import rclone_subprocess_env

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
    paths = config.get("paths") or {}
    data = Path(str(paths.get("data_dir") or Path("/opt/rclone-sync/data").resolve()))
    root = Path(
        str(paths.get("recovery_snapshots_dir") or data / "recovery-snapshots")
    ).expanduser()
    if not root.is_absolute():
        raise SnapshotError("Recovery-Ablage muss absolut sein")
    root = root.resolve()
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


def _inventory_local(
    root: Path, *, limit_bytes: int, hashes: bool = False
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

    for base, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        for name in sorted(dirs + names):
            item = Path(base) / name
            if item.is_symlink():
                raise SnapshotError(
                    "Symbolische Links werden im vollständigen Stand nicht unterstützt"
                )
            relative = _relative(item.relative_to(root).as_posix())
            if item.is_dir():
                directories.append(relative)
            elif item.is_file():
                stat = item.stat()
                total += stat.st_size
                files[relative] = {"size": stat.st_size, "modified": stat.st_mtime_ns}
                if hashes:
                    files[relative]["sha256"] = _hash_file(item)
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
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=rclone_subprocess_env(),
    )
    if result.returncode:
        raise SnapshotError(
            f"Recovery-Übertragung oder Zielprüfung fehlgeschlagen (exit {result.returncode})"
        )


def _inventory_remote(target: str, work: Path, limit_bytes: int) -> tuple[dict, list]:
    # Stream a bounded listing; a pathological remote must not exhaust RAM/disk.
    with tempfile.TemporaryFile(dir=work) as output:
        process = subprocess.Popen(
            ["rclone", "lsjson", "--recursive", "--", target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        timed_out = threading.Event()

        def kill_on_timeout() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(120, kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            assert process.stdout is not None
            with process.stdout:
                for block in iter(lambda: process.stdout.read(64 * 1024), b""):
                    if output.tell() + len(block) > MAX_MANIFEST_BYTES:
                        raise SnapshotError(
                            "Sicherungsbestand überschreitet das Manifestlimit"
                        )
                    output.write(block)
            process.wait(timeout=10)
            if process.returncode or timed_out.is_set():
                raise SnapshotError(
                    "Sicherungsbestand ist nicht vollständig lesbar oder das Zeitlimit wurde erreicht"
                )
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        output.seek(0)
        try:
            rows = json.load(output)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SnapshotError("Sicherungsziel lieferte ungültigen Bestand") from exc
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        raise SnapshotError("Sicherungsbestand überschreitet das Dateilimit")
    files, directories, total = {}, [], 0
    for row in rows:
        if not isinstance(row, dict):
            raise SnapshotError("Ungültiger Eintrag im Sicherungsbestand")
        relative = _relative(str(row.get("Path") or ""))
        if relative in files or relative in directories:
            raise SnapshotError("Doppelte Pfade im Sicherungsbestand")
        if row.get("IsDir"):
            directories.append(relative)
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
    work = Path(tempfile.mkdtemp(prefix=".capture-", dir=root))
    source, target = _endpoints(pair)
    try:
        before, directories = (
            _inventory_remote(target, work, limit)
            if _is_remote(target)
            else _inventory_local(
                Path(target).expanduser().resolve(), limit_bytes=limit, hashes=True
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
                ]
            )
        else:
            target_root = Path(target).expanduser().resolve()
            for relative in directories:
                (data / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
            for relative, info in before.items():
                origin = target_root / relative
                destination = data / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if origin.is_symlink() or not origin.resolve().is_relative_to(
                    target_root
                ):
                    raise SnapshotError(
                        "Sicherungsziel wurde während der Erfassung verändert"
                    )
                with origin.open("rb") as src, destination.open("xb") as dst:
                    remaining = info["size"]
                    while remaining:
                        block = src.read(min(1024 * 1024, remaining))
                        if not block:
                            raise SnapshotError(
                                "Sicherungsdatei wurde während der Erfassung verkürzt"
                            )
                        dst.write(block)
                        remaining -= len(block)
                    if src.read(1):
                        raise SnapshotError(
                            "Sicherungsdatei wurde während der Erfassung vergrößert"
                        )
                if _hash_file(destination) != info["sha256"]:
                    raise SnapshotError(
                        "Sicherungsdatei änderte sich während der Erfassung"
                    )
        captured, captured_dirs = _inventory_local(data, limit_bytes=limit, hashes=True)
        if set(captured) != set(before) or any(
            captured[p]["size"] != before[p]["size"] for p in before
        ):
            raise SnapshotError(
                "Vollständiger Stand enthält nicht alle erwarteten Dateien"
            )
        after, after_dirs = (
            _inventory_remote(target, work, limit)
            if _is_remote(target)
            else _inventory_local(
                Path(target).expanduser().resolve(), limit_bytes=limit, hashes=True
            )
        )
        if before != after or directories != after_dirs:
            raise SnapshotError(
                "Sicherungsbestand änderte sich während der Erfassung; erneut versuchen"
            )
        # Remote providers need not expose SHA-256; download-compare every captured file.
        if _is_remote(target) and captured:
            _run(["rclone", "check", "--download", "--", str(data), target])
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
        _atomic_json(work / "manifest.json", manifest)
        os.replace(work, root / point_id)
        return public_snapshot(manifest)
    finally:
        if work.exists() and work.parent == root:
            shutil.rmtree(work)


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
        )
    }


def load_manifest(
    config: Mapping[str, Any], pair: Mapping[str, Any], point_id: str
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
    if (
        not isinstance(value, dict)
        or value.get("schema") != "sicherpfad-full-snapshot-v1"
        or value.get("id") != point_id
        or value.get("fingerprint") != pair_fingerprint(pair)
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
    return sorted(result, key=lambda row: row["created_at"], reverse=True)[:180]


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
        result = {"ok": False, "error": str(exc)[:500]}
    database.job_finish(job_id, "ok" if result["ok"] else "error", result)
    database.audit_add(
        "recovery_snapshot_captured", actor="web", details={"job_id": job_id, **result}
    )
    return result


def run_snapshot_restore(
    database, config, pair, *, point_id: str, max_total_mb: int, job_id: int
) -> dict:
    from .jobs.selective_restore import _save_record, staging_root

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
        proposed = (
            Path(
                str(
                    (config.get("paths") or {}).get("recovery_dir")
                    or "/opt/rclone-sync/recovery"
                )
            )
            .expanduser()
            .resolve()
        )
        endpoints = [snapshot_root(config)]
        for configured in [*((config.get("backup") or {}).get("pairs") or []), pair]:
            if isinstance(configured, Mapping):
                endpoints.extend(
                    Path(path).expanduser().resolve()
                    for path in _endpoints(configured)
                    if path and not _is_remote(path)
                )
        if any(
            proposed == endpoint
            or proposed.is_relative_to(endpoint)
            or endpoint.is_relative_to(proposed)
            for endpoint in endpoints
        ):
            raise SnapshotError(
                "Recovery-Staging muss außerhalb aller Datenwege und vollständigen Stände liegen"
            )
        root = staging_root(config)
        work = root / record["id"]
        manifest = load_manifest(config, pair, point_id)
        total = manifest["total_bytes"]
        if total > max(1, min(max_total_mb, 51_200)) * 1024 * 1024:
            raise SnapshotError(
                "Vollständiger Stand überschreitet das Wiederherstellungslimit"
            )
        if shutil.disk_usage(root).free < total + 64 * 1024 * 1024:
            raise SnapshotError("Nicht genügend Speicher für die Wiederherstellung")
        source = Path(snapshot_target(config, pair, point_id))
        work.mkdir(mode=0o700)
        created = True
        data = work / "data"
        data.mkdir(mode=0o700)
        for directory in manifest["directories"]:
            (data / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in manifest["entries"]:
            origin = source / entry["path"]
            if origin.is_symlink() or not origin.resolve().is_relative_to(source):
                raise SnapshotError("Unsichere Datei im vollständigen Stand")
            if origin.stat().st_size != entry["size"]:
                raise SnapshotError("Gespeicherter Stand ist unvollständig")
            destination = data / entry["path"]
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
            if _hash_file(destination) != entry["sha256"]:
                raise SnapshotError(
                    "SHA-256 des wiederhergestellten Stands stimmt nicht"
                )
        actual, _dirs = _inventory_local(data, limit_bytes=MAX_BYTES, hashes=True)
        expected = {
            entry["path"]: {"size": entry["size"], "sha256": entry["sha256"]}
            for entry in manifest["entries"]
        }
        if {
            path: {"size": item["size"], "sha256": item["sha256"]}
            for path, item in actual.items()
        } != expected:
            raise SnapshotError("Wiederherstellung ist unvollständig")
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
        record.update(status="error", error=str(exc)[:500])
        if created and work is not None and work.exists() and work.parent == root:
            shutil.rmtree(work)
    _save_record(database, record)
    database.job_finish(job_id, "ok" if record["verified"] else "error", record)
    database.audit_add("full_recovery_finished", actor="web", details=record)
    return record
