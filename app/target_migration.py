"""Explicit copy/verify/cutover migrations; old targets are never deleted."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .auth import account_generation
from . import migration_lineage
from .config_store import ConfigConflictError
from .config_validation import _paths_overlap, validate_config
from .jobs import rclone_sync
from .jobs.job_lifecycle import BACKUP_KINDS, reconcile_locked_scope
from .jobs.locks import try_file_lock
from .jobs.recovery_lifecycle import (
    RecoveryCancelled,
    RecoveryPersistenceError,
    check_cancelled,
    finish_job,
    run_command,
)
from .jobs.restore_test import _endpoints
from .lockdown import is_locked_down
from .recovery_paths import recovery_path
from .recovery_points import _is_remote
from .recovery_walk import WalkLimitError, bounded_walk

JOB_KIND = "migration"
STATE_KEY = "target_migrations:v1"
MAX_ENTRIES = 20_000
MAX_OUTPUT = 16 * 1024 * 1024
MAX_PLANS = 50
PLAN_SECONDS = 24 * 3600
VERIFY_SECONDS = 15 * 60
COMMAND_SECONDS = 3600
_ID = re.compile(r"migration-[a-f0-9]{32}\Z")


class MigrationError(ValueError):
    pass


class MigrationBusy(MigrationError):
    pass


class MigrationLocked(MigrationBusy):
    pass


def _signature(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _overlaps(first: str, second: str) -> bool:
    if _is_remote(first) and _is_remote(second):
        a_name, a_path = first.split(":", 1)
        b_name, b_path = second.split(":", 1)
        if a_name.casefold() != b_name.casefold():
            return False
        a = PurePosixPath("/" + a_path.strip("/")).as_posix().casefold()
        b = PurePosixPath("/" + b_path.strip("/")).as_posix().casefold()
        return (
            a == "/"
            or b == "/"
            or a == b
            or a.startswith(b + "/")
            or b.startswith(a + "/")
        )
    return _paths_overlap(first, second)


def _relative(value: Any) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw == "."
        or len(raw) > 1024
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in raw
        or ":" in raw
        or any(ord(c) < 32 for c in raw)
    ):
        raise MigrationError("Ungültiger Dateipfad im Migrationsbestand")
    if path.as_posix() != raw:
        raise MigrationError("Nicht kanonischer Dateipfad im Migrationsbestand")
    return raw


def _local_security(endpoint: str, *, writable: bool = False) -> None:
    if _is_remote(endpoint):
        return
    path = Path(endpoint)
    if os.name != "posix":
        raise MigrationError("Lokale Zielmigration benötigt POSIX-Pfadschutz")
    current = Path(path.anchor)
    for part in (None, *path.parts[1:]):
        if part is not None:
            current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise MigrationError("Migration enthält unsichere lokale Verzeichnisse")
        if writable and info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise MigrationError(
                "Migrationsziel liegt in einem fremd beschreibbaren Ordner"
            )


def _inventory(endpoint: str, *, cancellable: bool = True) -> dict[str, dict]:
    _local_security(endpoint)
    local_paths = None
    if not _is_remote(endpoint):
        local_paths = set()

        def fail(error):
            raise MigrationError(
                "Lokaler Migrationsbestand ist nicht vollständig lesbar"
            ) from error

        try:
            for directory, folders, files in bounded_walk(
                Path(endpoint), max_entries=MAX_ENTRIES, onerror=fail
            ):
                for name in (*folders, *files):
                    path = directory / name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or not (
                        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                    ):
                        raise MigrationError(
                            "Links und Sonderdateien werden nicht migriert"
                        )
                    local_paths.add(path.relative_to(endpoint).as_posix())
        except WalkLimitError as exc:
            raise MigrationError(
                "Migrationsbestand überschreitet das Eintragslimit"
            ) from exc
    measured = run_command(
        ["rclone", "lsjson", "--recursive", "--", endpoint],
        timeout=60,
        max_output_bytes=MAX_OUTPUT,
        cancellable=cancellable,
    )
    if measured.returncode:
        raise MigrationError(
            "Migrationsbestand ist nicht vollständig lesbar; Ziel muss bereits existieren"
        )
    try:
        rows = json.loads(measured.stdout or "[]")
    except ValueError as exc:
        raise MigrationError("Migrationsbestand liefert ungültige Metadaten") from exc
    if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
        raise MigrationError("Migrationsbestand überschreitet das Eintragslimit")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("IsLink"):
            raise MigrationError("Links und Sonderdateien werden nicht migriert")
        name = _relative(row.get("Path"))
        directory = row.get("IsDir") is True
        size = row.get("Size")
        if not directory and (
            not isinstance(size, int) or isinstance(size, bool) or size < 0
        ):
            raise MigrationError("Dateigröße im Migrationsbestand ist unbekannt")
        if name in result:
            raise MigrationError("Doppelter Dateipfad im Migrationsbestand")
        if not _is_remote(endpoint):
            info = Path(endpoint).joinpath(*PurePosixPath(name).parts).lstat()
            if not (
                stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            ) or stat.S_ISLNK(info.st_mode):
                raise MigrationError("Links und Sonderdateien werden nicht migriert")
        result[name] = {
            "directory": directory,
            "size": 0 if directory else size,
            "modified": str(row.get("ModTime") or ""),
        }
    if local_paths is not None and set(result) != local_paths:
        raise MigrationError(
            "Lokaler Migrationsbestand wurde verändert oder vom Listing ausgelassen"
        )
    return result


def _hashes(endpoint: str, inventory: Mapping[str, dict]) -> dict[str, str]:
    files = {p for p, row in inventory.items() if not row["directory"]}
    measured = run_command(
        ["rclone", "hashsum", "SHA-256", "--download", "--", endpoint],
        timeout=COMMAND_SECONDS,
        max_output_bytes=MAX_OUTPUT,
    )
    if measured.returncode:
        raise MigrationError(
            "SHA-256-Leseprüfung des Migrationsbestands fehlgeschlagen"
        )
    result = {}
    for line in measured.stdout.splitlines():
        digest, separator, path = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
            or path not in files
            or path in result
        ):
            raise MigrationError("Unvollständiges SHA-256-Manifest der Migration")
        result[path] = digest.lower()
    if set(result) != files:
        raise MigrationError("Dateien fehlen in der SHA-256-Prüfung")
    return result


def _pair(config: Mapping[str, Any], identity: str) -> dict:
    for pair in (config.get("backup") or {}).get("pairs") or []:
        if isinstance(pair, dict) and identity in {
            str(pair.get("id") or ""),
            str(pair.get("name") or ""),
        }:
            return pair
    raise MigrationError("Datenweg nicht gefunden")


def _vault_guard(config: Mapping[str, Any], pair: Mapping[str, Any]) -> None:
    root = recovery_path(config, "device_vault_dir", "device-vault") / "records"
    if not root.exists():
        return
    if root.is_symlink():
        raise MigrationError("Unsichere Vault-Ablage")
    with os.scandir(root) as records:
        for index, entry in enumerate(records):
            if index >= MAX_ENTRIES:
                raise MigrationError("Vault-Bindungen überschreiten das Prüflimit")
            if not entry.name.endswith(".json"):
                continue
            info = os.stat(entry.path, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise MigrationError(
                    "Vault-Bindungen können nicht sicher geprüft werden"
                )
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise MigrationError(
                        "Vault-Bindung wurde während der Prüfung geändert"
                    )
                try:
                    record = json.loads(handle.read(1024 * 1024 + 1))
                except ValueError as exc:
                    raise MigrationError(
                        "Beschädigte Vault-Bindung vor Migration prüfen"
                    ) from exc
            if not isinstance(record, dict):
                raise MigrationError("Beschädigte Vault-Bindung vor Migration prüfen")
            if str(record.get("identity") or "") == str(
                pair.get("id") or pair.get("name")
            ) or str(record.get("pair") or "") == str(pair.get("name")):
                raise MigrationError(
                    "Datenweg besitzt Vault-Bindungen. Deren gesonderte Neuzuordnung ist vor einer Zielmigration erforderlich; Originaldaten bleiben erhalten."
                )


def _validate(config: Mapping[str, Any], pair: Mapping[str, Any], target: str) -> str:
    if str(pair.get("direction") or "bisync").lower() != "push":
        raise MigrationError(
            "Zielmigration unterstützt ausschließlich einseitige Push-Datenwege"
        )
    target = str(target or "").strip().rstrip("/")
    if (
        not target
        or len(target) > 1024
        or target.startswith("-")
        or any(ord(c) < 32 for c in target)
        or ".." in PurePosixPath(target.replace("\\", "/")).parts
    ):
        raise MigrationError("Ungültiges Migrationsziel")
    if _is_remote(target):
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:.+", target
        ) or not target.split(":", 1)[1].strip("/."):
            raise MigrationError(
                "Ein eigener Unterordner auf einem konfigurierten Remote ist erforderlich"
            )
    else:
        proposed = Path(target)
        roots = (config.get("web") or {}).get("local_browse_roots") or [
            "/mnt",
            "/media",
            "/srv",
        ]
        if not proposed.is_absolute() or not any(
            proposed.resolve().is_relative_to(Path(str(root)).resolve())
            for root in roots
        ):
            raise MigrationError(
                "Lokales Migrationsziel liegt außerhalb der erlaubten Ordner"
            )
        _local_security(target, writable=True)
    protected = []
    backup = config.get("backup") or {}
    for configured in backup.get("pairs") or []:
        if isinstance(configured, dict):
            protected.extend(_endpoints(configured))
            for key in ("backup_dir", "backup_dir1", "backup_dir2"):
                archive = (
                    str(configured.get(key) or backup.get(key) or "")
                    .split("{date}", 1)[0]
                    .rstrip("/")
                )
                if archive and (_is_remote(archive) or Path(archive).is_absolute()):
                    protected.append(archive)
    for pbs in (config.get("pbs") or {}).get("targets") or []:
        if isinstance(pbs, dict):
            protected.extend(str(path) for path in pbs.get("paths") or [])
    paths = config.get("paths") or {}
    protected.extend(
        str(p) for p in paths.values() if isinstance(p, str) and Path(p).is_absolute()
    )
    for endpoint in protected:
        if _overlaps(endpoint, target):
            raise MigrationError(
                "Migrationsziel überschneidet sich mit einem Datenweg oder einer internen Ablage"
            )
    for key in ("backup_dir", "backup_dir1", "backup_dir2"):
        value = str(pair.get(key) or backup.get(key) or "")
        if value and (_is_remote(value) or Path(value).is_absolute()):
            raise MigrationError(
                "Externe Versionsablagen erfordern eine gesonderte Migration; sie werden nicht stillschweigend ausgelassen"
            )
    if pair.get("airgap_device"):
        raise MigrationError(
            "Airgap-Datenwege benötigen eine gesonderte Geräteübergabe"
        )
    _vault_guard(config, pair)
    return target


def list_plans(database) -> list[dict]:
    rows = database.runtime_get(STATE_KEY, {})
    rows = rows if isinstance(rows, dict) else {}
    result = []
    for value in rows.values():
        if isinstance(value, dict):
            row = copy.deepcopy(value)
            if (
                row["status"] in {"preview", "verified"}
                and row["expires_at"] < time.time()
            ):
                row["status"] = "expired"
            if row["status"] in {"running", "switching"}:
                job = database.job_get(row.get("job_id")) or {}
                if job.get("status") not in {"running"}:
                    row["status"] = (
                        "cancelled" if job.get("status") == "cancelled" else "failed"
                    )
                    row["error"] = (
                        "Migrationslauf wurde beendet oder unterbrochen; erneute Vorschau erforderlich"
                    )
            for key in (
                "preview_signature",
                "verified_signature",
                "source_signature",
                "hash_signature",
            ):
                row.pop(key, None)
            result.append(row)
    return sorted(result, key=lambda p: p["created_at"], reverse=True)


def get_plan(database, plan_id: str, *, private: bool = False) -> dict:
    if not _ID.fullmatch(plan_id):
        raise MigrationError("Migrationsplan nicht gefunden")
    if not private:
        result = next((p for p in list_plans(database) if p["id"] == plan_id), None)
    else:
        result = (database.runtime_get(STATE_KEY, {}) or {}).get(plan_id)
    if not isinstance(result, dict):
        raise MigrationError("Migrationsplan nicht gefunden")
    return copy.deepcopy(result)


def _save(database, plan: dict, *, create: bool = False) -> dict:
    def update(current):
        rows = dict(current or {})
        if create:
            rows = {
                k: v
                for k, v in rows.items()
                if not (
                    v.get("status") == "preview"
                    and v.get("expires_at", 0) < time.time()
                )
            }
            if len(rows) >= MAX_PLANS:
                raise MigrationError("Zu viele gespeicherte Migrationspläne")
            for other in rows.values():
                if other.get("status") in {
                    "running",
                    "verified",
                    "switching",
                } and _overlaps(str(other.get("target") or ""), plan["target"]):
                    raise MigrationBusy(
                        "Das Ziel ist bereits durch einen Migrationsplan reserviert"
                    )
        rows[plan["id"]] = plan
        return rows

    database.runtime_update(STATE_KEY, update, default={})
    return get_plan(database, plan["id"])


def preview(
    store,
    database,
    *,
    identity: str,
    target: str,
    expected_revision: str,
    max_total_mb: int = 5120,
) -> dict:
    config, revision = store.snapshot_with_revision()
    if not expected_revision or revision != expected_revision:
        raise ConfigConflictError("Konfiguration wurde zwischenzeitlich geändert")
    pair = _pair(config, identity)
    target = _validate(config, pair, target)
    source = _endpoints(pair)[1]
    inventory = _inventory(source, cancellable=False)
    files = sum(not r["directory"] for r in inventory.values())
    total = sum(r["size"] for r in inventory.values())
    max_total_mb = max(1, min(int(max_total_mb), 51_200))
    if not files or total > max_total_mb * 1024 * 1024:
        raise MigrationError(
            "Quellziel ist leer oder überschreitet das bestätigte Größenlimit"
        )
    if _inventory(target, cancellable=False):
        raise MigrationError(
            "Migrationsziel muss bereits existieren und vollständig leer sein"
        )
    now = time.time()
    plan = {
        "id": "migration-" + uuid.uuid4().hex,
        "identity": str(pair.get("id") or pair["name"]),
        "pair": str(pair.get("name") or identity),
        "source": source,
        "target": target,
        "status": "preview",
        "config_revision": revision,
        "created_at": now,
        "expires_at": now + PLAN_SECONDS,
        "files": files,
        "total_bytes": total,
        "max_total_mb": max_total_mb,
        "preview_signature": _signature(inventory),
        "job_id": None,
        "verified_at": None,
        "switched_at": None,
        "error": None,
        "warnings": [
            "Kopiert alle regulären Dateien und Ordner des alten Ziels einschließlich relativer Versionsablagen; das alte Ziel wird nicht gelöscht.",
            "SHA-256-Leseprüfungen lesen sämtliche Dateiinhalte an beiden Zielen und können Cloud-Egress verursachen.",
            "Die Umschaltung ist ein eigener bestätigter Schritt nach erfolgreicher Prüfung.",
        ],
    }
    return _save(database, plan, create=True)


def _bound(store, plan: Mapping[str, Any]) -> tuple[dict, dict]:
    config, revision = store.snapshot_with_revision()
    if revision != plan["config_revision"]:
        raise ConfigConflictError(
            "Konfiguration seit der Migrationsvorschau geändert; neue Vorschau erforderlich"
        )
    pair = _pair(config, str(plan["identity"]))
    if _endpoints(pair)[1] != plan["source"]:
        raise ConfigConflictError("Migrationsquelle wurde geändert")
    _validate(config, pair, str(plan["target"]))
    if plan["expires_at"] < time.time():
        raise MigrationError("Migrationsplan ist abgelaufen")
    return config, pair


def _lease(database):
    lease = try_file_lock("backup")
    if lease is None:
        raise MigrationBusy(
            "Eine Sicherung, Wiederherstellung oder Migration läuft bereits"
        )
    try:
        if not reconcile_locked_scope(
            database, scope="backup", kinds=(*BACKUP_KINDS, JOB_KIND)
        ).get("safe"):
            raise MigrationBusy("Ein registrierter Sicherungsprozess ist noch aktiv")
        rclone_sync.reset_cancel()
        if is_locked_down(database):
            raise MigrationLocked("Lockdown aktiv")
        return lease
    except BaseException:
        lease.release()
        raise


def _verified_state(
    source: str,
    target: str,
    expected_source: Mapping[str, dict],
    hashes: Mapping[str, str],
) -> dict:
    before = _inventory(source)
    destination = _inventory(target)
    if before != expected_source or set(destination) != set(before):
        raise MigrationError(
            "Quelle oder Zielpfade haben sich während der Migration geändert"
        )
    if any(
        destination[p]["directory"] != row["directory"]
        or destination[p]["size"] != row["size"]
        for p, row in before.items()
    ):
        raise MigrationError("Migrationsziel ist unvollständig")
    source_hashes, target_hashes = _hashes(source, before), _hashes(target, destination)
    if source_hashes != hashes or target_hashes != hashes:
        raise MigrationError(
            "SHA-256-Prüfung stimmt nicht mit dem Quellbestand überein"
        )
    if _inventory(source) != before or _inventory(target) != destination:
        raise MigrationError("Dateibestand wurde während der Leseprüfung geändert")
    return destination


def _run_worker(store, database, plan: dict, lease) -> None:
    job_id = plan["job_id"]
    try:
        _bound(store, plan)
        original = _inventory(plan["source"])
        if _signature(original) != plan["preview_signature"] or _inventory(
            plan["target"]
        ):
            raise MigrationError(
                "Quelle geändert oder Ziel nicht mehr leer; neue Vorschau erforderlich"
            )
        hashes = _hashes(plan["source"], original)
        _local_security(plan["target"], writable=True)
        copied = run_command(
            [
                "rclone",
                "copy",
                "--immutable",
                "--create-empty-src-dirs",
                "--retries",
                "1",
                "--low-level-retries",
                "1",
                "--max-transfer",
                str(plan["max_total_mb"] * 1024 * 1024),
                "--stats",
                "0",
                "--",
                plan["source"],
                plan["target"],
            ],
            timeout=COMMAND_SECONDS,
            max_output_bytes=1024 * 1024,
        )
        if copied.returncode:
            raise MigrationError(
                "Kopie fehlgeschlagen; Original und bereits kopierte Zieldateien bleiben erhalten"
            )
        destination = _verified_state(plan["source"], plan["target"], original, hashes)
        _bound(store, plan)
        check_cancelled()
        now = time.time()
        plan.update(
            status="verified",
            verified_at=now,
            expires_at=now + VERIFY_SECONDS,
            source_signature=_signature(original),
            verified_signature=_signature(destination),
            hash_signature=_signature(hashes),
        )
        _save(database, plan)
        finish_job(
            database,
            job_id,
            "ok",
            {
                "ok": True,
                "migration_id": plan["id"],
                "files": plan["files"],
                "bytes": plan["total_bytes"],
                "verification": "sha256_readback",
                "old_target_retained": True,
            },
        )
    except Exception as exc:
        plan.update(
            status="cancelled" if isinstance(exc, RecoveryCancelled) else "failed",
            error=str(exc)[:500],
        )
        try:
            _save(database, plan)
        finally:
            try:
                finish_job(
                    database,
                    job_id,
                    "cancelled" if isinstance(exc, RecoveryCancelled) else "error",
                    {"ok": False, "migration_id": plan["id"], "error": str(exc)[:500]},
                )
            except RecoveryPersistenceError:
                pass
    finally:
        lease.release()


def start(store, database, plan_id: str, *, background: bool = True) -> dict:
    lease = _lease(database)
    handed_off = False
    try:
        plan = get_plan(database, plan_id, private=True)
        if plan["status"] != "preview":
            raise MigrationError(
                "Nur ein noch nicht gestarteter Vorschauplan kann ausgeführt werden"
            )
        _bound(store, plan)
        job_id = database.job_start(
            JOB_KIND,
            trigger="manual",
            exclusive_scope=True,
            config_revision=plan["config_revision"],
        )
        plan.update(status="running", job_id=job_id)
        try:
            response = _save(database, plan)
        except BaseException:
            finish_job(
                database,
                job_id,
                "error",
                {
                    "ok": False,
                    "error": "Migrationsstart konnte nicht gespeichert werden",
                },
            )
            raise
        if background:
            try:
                threading.Thread(
                    target=_run_worker,
                    args=(store, database, plan, lease),
                    name="target-migration",
                    daemon=True,
                ).start()
                handed_off = True
            except BaseException as exc:
                plan.update(
                    status="failed", error="Migration konnte nicht gestartet werden"
                )
                _save(database, plan)
                finish_job(
                    database, job_id, "error", {"ok": False, "error": str(exc)[:500]}
                )
                raise
        else:
            handed_off = True
            _run_worker(store, database, plan, lease)
        return response if background else get_plan(database, plan_id)
    except BaseException:
        if not handed_off:
            lease.release()
        raise


def recover_cutovers(store, database) -> None:
    """Repair the metadata half if config commit succeeded before DB persistence."""
    with store.locked_snapshot(exclusive=True) as snapshot:
        revision = store.revision
        migration_lineage.commit(snapshot, revision)
        committed_ids = migration_lineage.committed_ids(snapshot)

    def update(current):
        rows = dict(current or {})
        for old in rows.values():
            if old.get("cutover_requested_at") and old.get("status") in {
                "verified",
                "switching",
                "failed",
                "cancelled",
            }:
                try:
                    pair = _pair(snapshot, str(old["identity"]))
                except MigrationError:
                    continue
                if _endpoints(pair)[1] == old["target"] and old["id"] in committed_ids:
                    old.update(
                        status="switched",
                        switched_at=old["cutover_requested_at"],
                        config_revision=revision,
                    )
        return rows

    database.runtime_update(STATE_KEY, update, default={})


def _cutover(store, database, plan, *, expected_revision, expected_generation=None):
    copy_job_id = plan.get("copy_job_id") or plan.get("job_id")
    if plan["status"] == "switched":
        return get_plan(database, plan["id"])
    if (
        plan["status"] not in {"verified", "switching"}
        or (database.job_get(copy_job_id) or {}).get("status") != "ok"
    ):
        raise MigrationError(
            "Umschalten erst nach dauerhaft bestätigter, erfolgreicher SHA-256-Prüfung"
        )
    _bound(store, plan)
    if expected_revision != plan["config_revision"]:
        raise ConfigConflictError(
            "Konfigurationsrevision stimmt nicht mit dem Migrationsplan überein"
        )
    source, target = _inventory(plan["source"]), _inventory(plan["target"])
    if (
        _signature(source) != plan["source_signature"]
        or _signature(target) != plan["verified_signature"]
    ):
        raise MigrationError(
            "Quelle oder Ziel wurde nach der Prüfung verändert; neue Migration erforderlich"
        )
    hashes = _hashes(plan["source"], source)
    if _signature(hashes) != plan["hash_signature"]:
        raise MigrationError("Quellinhalt wurde seit der Prüfung verändert")
    _verified_state(plan["source"], plan["target"], source, hashes)
    check_cancelled()
    plan["cutover_requested_at"] = time.time()
    _save(database, plan)

    def update(current):
        check_cancelled()
        if is_locked_down(database):
            raise MigrationLocked("Lockdown aktiv; Ziel wurde nicht umgeschaltet")
        if (
            expected_generation is not None
            and account_generation(current) != expected_generation
        ):
            raise ConfigConflictError(
                "Passwortbestätigung wurde durch eine Kontoänderung widerrufen"
            )
        configured = _pair(current, str(plan["identity"]))
        _validate(current, configured, str(plan["target"]))
        if _endpoints(configured)[1] != plan["source"]:
            raise ConfigConflictError("Migrationsquelle wurde geändert")
        previous_pair = copy.deepcopy(configured)
        # Normalize while the old endpoint still defines a stable ID, then keep
        # that ID when switching the target (including legacy raw config).
        normalized, _warnings = validate_config(current)
        configured = _pair(normalized, str(plan["identity"]))
        configured["remote"] = plan["target"]
        validated, _warnings = validate_config(normalized)
        following_pair = _pair(validated, str(configured["id"]))
        migration_lineage.prepare(
            validated,
            previous_pair,
            following_pair,
            migration_id=plan["id"],
            committed_revision=migration_lineage.config_revision(validated),
        )
        return validated

    store.update(update, expected_revision=expected_revision)
    with store.locked_snapshot(exclusive=True) as committed:
        migration_lineage.commit(committed, store.revision, plan["id"])
    plan.update(
        status="switched", switched_at=time.time(), config_revision=store.revision
    )
    _save(database, plan)
    try:
        database.audit_add(
            "target_migration_switched",
            actor="web",
            details={
                "migration_id": plan["id"],
                "identity": plan["identity"],
                "old_target_retained": True,
            },
        )
    except Exception:
        # Config is already committed. Audit failure must not report false failure.
        pass
    return get_plan(database, plan["id"])


def switch(
    store,
    database,
    plan_id: str,
    *,
    expected_revision: str,
    expected_generation: str | None = None,
) -> dict:
    """Synchronous domain entry point; HTTP uses the background variant below."""
    recover_cutovers(store, database)
    lease = _lease(database)
    try:
        return _cutover(
            store,
            database,
            get_plan(database, plan_id, private=True),
            expected_revision=expected_revision,
            expected_generation=expected_generation,
        )
    finally:
        lease.release()


def _switch_worker(
    store, database, plan, lease, expected_revision, expected_generation
):
    status, summary = (
        "error",
        {"ok": False, "migration_id": plan["id"], "phase": "cutover"},
    )
    try:
        _cutover(
            store,
            database,
            plan,
            expected_revision=expected_revision,
            expected_generation=expected_generation,
        )
        status, summary = (
            "ok",
            {
                **summary,
                "ok": True,
                "old_target_retained": True,
                "verification": "sha256_readback",
            },
        )
    except Exception as exc:
        # A config commit and its plan metadata are two durable stores. Recover
        # an already committed cutover instead of claiming that it was undone.
        recover_cutovers(store, database)
        actual = get_plan(database, plan["id"], private=True)
        if actual["status"] == "switched":
            status, summary = "ok", {**summary, "ok": True, "old_target_retained": True}
        else:
            status = "cancelled" if isinstance(exc, RecoveryCancelled) else "error"
            plan.update(
                status="cancelled" if status == "cancelled" else "failed",
                error=str(exc)[:500],
            )
            _save(database, plan)
            summary["error"] = str(exc)[:500]
    finally:
        try:
            finish = getattr(database, "job_finish_external", database.job_finish)
            if not finish(plan["job_id"], status, summary):
                raise RecoveryPersistenceError(
                    "Abschluss der Zielumschaltung wurde nicht dauerhaft bestätigt"
                )
        finally:
            lease.release()


def start_switch(
    store, database, plan_id: str, *, expected_revision: str, expected_generation: str
) -> dict:
    """Reserve the backup scope before returning an asynchronously polled cutover."""
    recover_cutovers(store, database)
    lease = _lease(database)
    handed_off = False
    try:
        plan = get_plan(database, plan_id, private=True)
        if plan["status"] == "switched":
            return get_plan(database, plan_id)
        if plan["status"] != "verified":
            raise MigrationError("Die Migration ist noch nicht erfolgreich verifiziert")
        config, _pair_value = _bound(store, plan)
        if (
            expected_revision != plan["config_revision"]
            or account_generation(config) != expected_generation
        ):
            raise ConfigConflictError(
                "Konfiguration oder Passwortbestätigung wurde geändert"
            )
        copy_job_id = plan.get("copy_job_id") or plan["job_id"]
        if (database.job_get(copy_job_id) or {}).get("status") != "ok":
            raise MigrationError("Der Kopierlauf ist noch nicht dauerhaft bestätigt")
        job_id = database.job_start(
            JOB_KIND,
            trigger="manual",
            exclusive_scope=True,
            config_revision=expected_revision,
        )
        plan.update(status="switching", copy_job_id=copy_job_id, job_id=job_id)
        try:
            response = _save(database, plan)
            threading.Thread(
                target=_switch_worker,
                args=(
                    store,
                    database,
                    plan,
                    lease,
                    expected_revision,
                    expected_generation,
                ),
                name="target-cutover",
                daemon=True,
            ).start()
            handed_off = True
            return response
        except BaseException as exc:
            plan.update(
                status="failed", error="Umschaltung konnte nicht gestartet werden"
            )
            _save(database, plan)
            database.job_finish(job_id, "error", {"ok": False, "error": str(exc)[:500]})
            raise
    finally:
        if not handed_off:
            lease.release()
