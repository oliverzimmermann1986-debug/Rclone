"""Wartungs-APIs für Logs und einen redigierten Konfigurations-Export."""

from __future__ import annotations

import hashlib
import heapq
import io
import json
import os
import re
import secrets
import stat
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..auth import require_auth, verify_password
from ..config_store import ConfigConflictError, get_config
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..maintenance import iter_logs, logs_root, prune_logs as prune_log_files
from ..security import require_csrf
from ..system_info import system_snapshot

router = APIRouter(
    prefix="/api/maintenance",
    tags=["maintenance"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/logs")
def list_logs(
    limit: int = Query(200, ge=1, le=1000), query: str = Query("", max_length=200)
) -> dict[str, Any]:
    root = logs_root()
    needle = query.casefold().strip()

    def matching_logs():
        for path in iter_logs(root):
            try:
                relative = str(path.relative_to(root))
                if needle and needle not in relative.casefold():
                    continue
                stat_result = path.stat()
                yield {
                    "path": relative,
                    "size": stat_result.st_size,
                    "mtime": stat_result.st_mtime,
                }
            except OSError:
                continue

    files = heapq.nlargest(limit, matching_logs(), key=lambda item: item["mtime"])
    return {"root": str(root), "logs": files}


@router.post("/logs/prune")
def prune_logs(
    days: int = Query(30, ge=1, le=3650), dry_run: bool = True
) -> dict[str, Any]:
    return prune_log_files(days=days, dry_run=dry_run)


@router.get("/audit")
def audit_events(
    limit: int = Query(100, ge=1, le=1000),
    event_type: str = Query("", max_length=80),
) -> dict[str, Any]:
    return {
        "ok": True,
        "events": get_db().audit_list(
            limit=limit, event_type=event_type.strip() or None
        ),
    }


@router.get("/database")
def database_status() -> dict[str, Any]:
    db = get_db()
    return {"ok": True, "stats": db.stats(), "integrity": db.integrity_check()}


@router.post("/database/prune")
def prune_database(
    days: int = Query(180, ge=1, le=3650),
    keep_latest: int = Query(500, ge=10, le=100000),
) -> dict[str, Any]:
    db = get_db()
    deleted = db.jobs_prune(days, keep_latest)
    auth_deleted = db.auth_prune(7)
    audit_deleted = db.audit_prune(max(days, 365), max(keep_latest, 1000))
    db.checkpoint()
    return {
        "ok": True,
        "deleted_jobs": deleted,
        "deleted_auth_rows": auth_deleted,
        "deleted_audit_events": audit_deleted,
        "stats": db.stats(),
    }


def _redacted_export() -> dict[str, Any]:
    config = get_config().snapshot()
    web = config.setdefault("web", {})
    if isinstance(web, dict):
        for key in ("password", "password_hash", "secret_key"):
            if key in web:
                web[key] = "***REDACTED***"
    notifications = config.get("notifications")
    if isinstance(notifications, dict):
        hooks = notifications.get("webhooks")
        if isinstance(hooks, list):
            for hook in hooks:
                if isinstance(hook, dict) and hook.get("url"):
                    hook["url"] = "***REDACTED***"
    return config


@router.get("/config/export")
def export_config() -> Response:
    body = yaml.safe_dump(_redacted_export(), allow_unicode=True, sort_keys=False)
    return Response(
        body,
        media_type="text/yaml; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=rclone-sync-config-redacted.yaml",
            "Cache-Control": "no-store",
        },
    )


_SNAPSHOT_NAME_RE = re.compile(
    r"^(?:config|pre-restore)-\d{8}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}\.yaml$"
)
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_MAX_SNAPSHOTS = 30


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_dir() -> Path:
    data_dir = Path(
        get_config().get("paths", "data_dir", default="/opt/rclone-sync/data")
    ).resolve()
    root = data_dir / "config-snapshots"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, stat.S_IRWXU)
    except OSError:
        pass
    return root


def _snapshot_name(prefix: str, revision: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{revision[:8]}-{secrets.token_hex(2)}.yaml"


def _write_snapshot(
    data: dict[str, Any], revision: str, *, prefix: str = "config"
) -> dict[str, Any]:
    root = _snapshot_dir()
    raw = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
    if len(raw) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(413, "Konfiguration ist für einen Snapshot zu groß")
    name = _snapshot_name(prefix, revision)
    path = root / name
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _prune_snapshots(root)
    info = path.stat()
    return {
        "name": name,
        "size": info.st_size,
        "mtime": info.st_mtime,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _snapshot_entries(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or _snapshot_dir()
    entries: list[dict[str, Any]] = []
    for path in root.iterdir():
        try:
            if (
                not _SNAPSHOT_NAME_RE.fullmatch(path.name)
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            info = path.stat()
            digest = _file_sha256(path)
            entries.append(
                {
                    "name": path.name,
                    "size": info.st_size,
                    "mtime": info.st_mtime,
                    "sha256": digest,
                }
            )
        except OSError:
            continue
    entries.sort(key=lambda item: float(item["mtime"]), reverse=True)
    return entries


def _prune_snapshots(root: Path) -> None:
    for entry in _snapshot_entries(root)[_MAX_SNAPSHOTS:]:
        try:
            (root / str(entry["name"])).unlink()
        except OSError:
            continue


@router.get("/config/snapshots")
def list_config_snapshots() -> dict[str, Any]:
    return {
        "ok": True,
        "snapshots": _snapshot_entries(),
        "max_snapshots": _MAX_SNAPSHOTS,
    }


@router.post("/config/snapshots")
def create_config_snapshot() -> dict[str, Any]:
    data, revision = get_config().snapshot_with_revision()
    try:
        normalized, _warnings = validate_config(data)
    except ConfigValidationError as exc:
        raise HTTPException(
            422,
            {"message": "Aktuelle Konfiguration ist ungültig", "errors": exc.errors},
        )
    entry = _write_snapshot(normalized, revision)
    get_db().audit_add(
        "config_snapshot_created", actor="web", details={"name": entry["name"]}
    )
    return {"ok": True, "snapshot": entry}


class SnapshotRestore(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    current_password: str = Field(min_length=1, max_length=1024)
    expected_revision: str = Field(min_length=1, max_length=128)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@router.post("/config/snapshots/restore")
def restore_config_snapshot(
    body: SnapshotRestore, user: str = Depends(require_auth)
) -> dict[str, Any]:
    if not verify_password(user, body.current_password):
        raise HTTPException(403, "Aktuelles Passwort falsch")
    if not _SNAPSHOT_NAME_RE.fullmatch(body.name):
        raise HTTPException(400, "Ungültiger Snapshot-Name")
    root = _snapshot_dir()
    path = root / body.name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise HTTPException(404, "Snapshot nicht gefunden")
    except OSError as exc:
        raise HTTPException(400, f"Snapshot konnte nicht geöffnet werden: {exc}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SNAPSHOT_BYTES:
            raise HTTPException(400, "Snapshot ist keine gültige Konfigurationsdatei")
        raw = b""
        while len(raw) <= _MAX_SNAPSHOT_BYTES:
            chunk = os.read(fd, min(65536, _MAX_SNAPSHOT_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    digest = hashlib.sha256(raw).hexdigest()
    if body.sha256 and not secrets.compare_digest(body.sha256, digest):
        raise HTTPException(409, "Snapshot wurde seit der Auswahl verändert")
    try:
        loaded = yaml.safe_load(raw.decode("utf-8")) or {}
        normalized, warnings = validate_config(loaded)
    except (UnicodeError, yaml.YAMLError, ConfigValidationError, ValueError) as exc:
        errors = exc.errors if isinstance(exc, ConfigValidationError) else [str(exc)]
        raise HTTPException(422, {"message": "Snapshot ist ungültig", "errors": errors})

    store = get_config()
    current, revision = store.snapshot_with_revision()
    if body.expected_revision != revision:
        raise HTTPException(
            409,
            {
                "message": "Konfiguration wurde parallel geändert",
                "current_revision": revision,
            },
        )
    _write_snapshot(current, revision, prefix="pre-restore")

    current_web = current.get("web") if isinstance(current.get("web"), dict) else {}
    restored_web = normalized.setdefault("web", {})
    for key in ("username", "password", "password_hash", "secret_key"):
        restored_web[key] = current_web.get(key, "")
    try:
        session_version = int(current_web.get("session_version", 1) or 1)
    except (TypeError, ValueError):
        session_version = 1
    restored_web["session_version"] = max(1, session_version) + 1
    try:
        new_revision = store.replace(normalized, expected_revision=revision)
    except ConfigConflictError:
        raise HTTPException(409, "Konfiguration wurde parallel geändert")
    get_db().audit_add(
        "config_snapshot_restored",
        actor=user,
        details={"name": body.name, "revision": new_revision},
    )
    return {
        "ok": True,
        "warnings": warnings,
        "revision": new_revision,
        "reauthenticate": True,
    }


@router.get("/support-bundle")
def support_bundle() -> Response:
    config = _redacted_export()
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    db = get_db()
    logs = list_logs(limit=250, query="")
    diagnostics = {
        "generated_at": time.time(),
        "app_version": __version__,
        "system": system_snapshot(
            str(paths.get("data_dir") or "/opt/rclone-sync/data")
        ),
        "database": {"stats": db.stats(), "integrity": db.integrity_check()},
        "recent_jobs": db.job_list(limit=100),
        "recent_audit_events": db.audit_list(limit=100),
        "log_inventory": logs,
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(
        payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        archive.writestr(
            "README.txt",
            "rclone-sync Support-Bundle\n\nEnthält keine Passwörter, Session-Secrets oder Webhook-URLs. "
            "Lokale Pfade, Remote-Namen und Job-Fehler können zur Diagnose enthalten sein.\n",
        )
        archive.writestr(
            "config-redacted.yaml",
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        )
        archive.writestr(
            "diagnostics.json",
            json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str),
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return Response(
        payload.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=rclone-sync-support-{stamp}.zip",
            "Cache-Control": "no-store",
        },
    )
