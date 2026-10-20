"""Recovery Center: evidence, quarantine and isolated selective restores."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import PurePosixPath
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import BaseModel, Field

from .. import __version__, protection
from ..auth import require_auth, require_reauthentication
from ..config_store import get_config
from ..db import JobAlreadyRunningError, get_db
from ..jobs.restore_test import _endpoints
from ..jobs import runtime_state
from ..jobs.locks import HeldFileLock, try_file_lock
from ..jobs.selective_restore import (
    JOB_KIND,
    list_staging,
    normalize_selection,
    remove_staging,
    run_selective_restore,
)
from ..rclone_args import rclone_subprocess_env
from ..recovery_points import (
    RecoveryPointError,
    _is_remote,
    _local_browse,
    browse_point,
    compare_points,
    list_points,
    point_target,
)
from ..secret_redaction import REDACTED, redact_secrets
from ..security import require_csrf
from ..handover_rescue import (
    HandoverError,
    MAX_PACKAGE_BYTES,
    import_handover,
    portable_inventory,
    preview_handover,
)
from ..recovery_snapshots import (
    SnapshotError,
    load_manifest,
    run_snapshot_capture,
    run_snapshot_restore,
)
from . import api_diagnostics, api_storage

router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
logger = logging.getLogger(__name__)


def _pairs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    backup = config.get("backup") if isinstance(config.get("backup"), Mapping) else {}
    return [
        dict(item)
        for item in (backup or {}).get("pairs", [])
        if isinstance(item, Mapping)
    ]


def _find_pair(config: Mapping[str, Any], identity: str) -> dict[str, Any]:
    needle = str(identity or "").strip()
    for pair in _pairs(config):
        if needle in {str(pair.get("id") or ""), str(pair.get("name") or "")}:
            return pair
    raise HTTPException(404, "Datenweg nicht gefunden")


def _public_path(value: Any, include_paths: bool) -> str:
    return str(value or "") if include_paths else REDACTED


def build_recovery_pass(*, include_paths: bool = False) -> dict[str, Any]:
    config = get_config().snapshot()
    overview = api_diagnostics.overview()
    storage = api_storage.overview(include_remote=False, refresh_sizes=False)
    scored = protection.score_components(
        overview=overview, storage=storage, config=config
    )
    now = time.time()
    data_paths = []
    configured_by_name = {str(item.get("name") or ""): item for item in _pairs(config)}
    for item in storage.get("pairs") or []:
        last_sync = item.get("last_sync")
        evidence = item.get("restore_evidence") or {}
        data_paths.append(
            {
                "name": str(item.get("name") or ""),
                "direction": str(item.get("direction") or ""),
                "enabled": bool(
                    configured_by_name.get(str(item.get("name") or ""), {}).get(
                        "enabled", True
                    )
                ),
                "source": _public_path(item.get("source"), include_paths),
                "target": _public_path(item.get("target"), include_paths),
                "last_sync_at": last_sync,
                "rpo_seconds": max(0, round(now - float(last_sync)))
                if last_sync
                else None,
                "restore": evidence,
            }
        )
    database = get_db()
    quarantine = protection.anomaly_status(database, _pairs(config))
    recent_audit = []
    for event in database.audit_list(limit=20):
        item = {
            "id": event.get("id"),
            "event_type": event.get("event_type"),
            "actor": event.get("actor"),
            "created_at": event.get("created_at"),
        }
        if include_paths:
            item["details"] = redact_secrets(event.get("details") or {})
        recent_audit.append(item)
    return {
        "schema": "rclone-recovery-pass-v1",
        "generated_at": now,
        "app_version": __version__,
        "hostname": str((overview.get("system") or {}).get("hostname") or ""),
        "protection": scored,
        "data_paths": data_paths,
        "quarantine": quarantine,
        "recent_audit": recent_audit,
        "paths_included": include_paths,
    }


def encrypted_handover(payload: Mapping[str, Any], passphrase: str) -> dict[str, Any]:
    if len(passphrase) < 12 or len(passphrase) > 1024:
        raise ValueError("Übergabe-Passphrase muss 12 bis 1024 Zeichen lang sein")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    iterations = 600_000
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    ).derive(passphrase.encode("utf-8"))
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(plaintext) > MAX_PACKAGE_BYTES:
        raise ValueError("Notfallakte überschreitet das zulässige Größenlimit")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, b"rclone-recovery-handover-v1")
    return {
        "schema": "rclone-recovery-handover-v1",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": iterations,
        "cipher": "AES-256-GCM",
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
    }


@router.get("/pass")
def recovery_pass(include_paths: bool = False) -> dict[str, Any]:
    return build_recovery_pass(include_paths=include_paths)


@router.get("/pass/export")
def export_recovery_pass(include_paths: bool = False) -> Response:
    body = json.dumps(
        build_recovery_pass(include_paths=include_paths), ensure_ascii=False, indent=2
    )
    get_db().audit_add(
        "recovery_pass_exported", actor="web", details={"paths_included": include_paths}
    )
    return Response(
        body,
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=recovery-pass.json",
            "Cache-Control": "no-store",
        },
    )


@router.get("/calendar")
def recovery_calendar(days: int = Query(90, ge=7, le=366)) -> dict[str, Any]:
    config = get_config().snapshot()
    timezone = str((config.get("backup") or {}).get("timezone") or "Europe/Berlin")
    return {
        "days": protection.protection_calendar(
            get_db(), days=days, timezone_name=timezone
        ),
        "timezone": timezone,
    }


@router.get("/policies")
def policy_profiles() -> dict[str, Any]:
    return {"profiles": list(protection.POLICY_PRESETS)}


@router.get("/quarantine")
def quarantine_status() -> dict[str, Any]:
    config = get_config().snapshot()
    return protection.anomaly_status(get_db(), _pairs(config))


class ReauthenticationBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)


@router.post("/quarantine/{identity}/acknowledge")
def acknowledge_quarantine(
    identity: str,
    body: ReauthenticationBody,
    request: Request,
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    require_reauthentication(request, user, body.current_password)
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    if not protection.acknowledge_quarantine(get_db(), pair):
        raise HTTPException(404, "Für diesen Datenweg besteht keine Quarantäne")
    return {"ok": True, "pair": str(pair.get("name") or "")}


class HandoverRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    passphrase: str = Field(min_length=12, max_length=1024)
    include_paths: bool = False


@router.post("/handover")
def create_handover(
    body: HandoverRequest,
    request: Request,
    user: str = Depends(require_auth),
) -> Response:
    require_reauthentication(request, user, body.current_password)
    config = get_config().snapshot()
    try:
        package = {
            "created_at": time.time(),
            "recovery_pass": build_recovery_pass(include_paths=body.include_paths),
            "rescue_inventory": portable_inventory(
                config, include_paths=body.include_paths
            ),
            "instructions": [
                "Dieses Paket enthält keine Anmeldedaten oder Cloud-Schlüssel.",
                "Passphrase getrennt und sicher an die bevollmächtigte Person übermitteln.",
                "Wiederherstellungen zuerst in einen getrennten Staging-Ordner durchführen.",
            ],
        }
        envelope = encrypted_handover(package, body.passphrase)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    get_db().audit_add(
        "recovery_handover_exported",
        actor=user,
        details={"paths_included": body.include_paths},
    )
    return Response(
        json.dumps(envelope, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=recovery-handover.encrypted.json",
            "Cache-Control": "no-store",
        },
    )


class OpenHandoverRequest(BaseModel):
    envelope: dict[str, Any]
    passphrase: str = Field(min_length=12, max_length=1024)


class ImportHandoverRequest(OpenHandoverRequest):
    current_password: str = Field(min_length=1, max_length=1024)
    mappings: dict[str, str] = Field(max_length=500)


@router.post("/handover/preview")
def preview_rescue(body: OpenHandoverRequest) -> dict[str, Any]:
    try:
        return preview_handover(get_config().snapshot(), body.envelope, body.passphrase)
    except HandoverError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/handover/import")
def import_rescue(
    body: ImportHandoverRequest, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    require_reauthentication(request, user, body.current_password)
    try:
        result = import_handover(
            get_config().snapshot(), body.envelope, body.passphrase, body.mappings
        )
    except HandoverError as exc:
        raise HTTPException(400, str(exc)) from exc
    get_db().audit_add(
        "recovery_handover_imported",
        actor=user,
        details={
            "imported": result["imported"],
            "already_present": result["already_present"],
        },
    )
    return result


class SnapshotRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=128)
    max_total_mb: int = Field(default=5120, ge=1, le=51_200)


def _finish_failed_recovery(database, job_id: int, error: BaseException) -> None:
    try:
        database.job_finish(job_id, "error", {"ok": False, "error": str(error)[:500]})
    except Exception:
        logger.exception("Recovery-Job %s konnte nicht abgeschlossen werden", job_id)


def _release_recovery(lease: HeldFileLock, run_id: str, status: str) -> None:
    try:
        runtime_state.finish_run(run_id, status)
    except Exception:
        logger.exception(
            "Recovery-Runtime %s konnte nicht abgeschlossen werden", run_id
        )
    finally:
        lease.release()


def _run_recovery_locked(lease, run_id, function, database, *args, job_id, **kwargs):
    status = "error"
    try:
        result = function(database, *args, job_id=job_id, **kwargs)
        job = database.job_get(job_id) or {}
        status = str(job.get("status") or "error")
        if status == "running":
            raise RuntimeError("Recovery-Worker hat den Job nicht abgeschlossen")
        return result
    except BaseException as exc:
        status = "error"
        _finish_failed_recovery(database, job_id, exc)
        try:
            current = database.job_get(job_id) or {}
            if current.get("status") and current["status"] != "running":
                status = str(current["status"])
        except Exception:
            logger.exception(
                "Recovery-Jobstatus %s konnte nicht gelesen werden", job_id
            )
        raise
    finally:
        _release_recovery(lease, run_id, status)


def _start_recovery_job(pair) -> tuple[Any, int, HeldFileLock, str]:
    lease = try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
    if lease is None:
        raise HTTPException(
            409,
            "Während einer Sicherung oder Recovery läuft bereits ein anderer Prozess",
        )
    database = None
    job_id = None
    try:
        database = get_db()
        if runtime_state.active_processes(runtime_state.DEFAULT_CANCEL_SCOPE):
            raise HTTPException(
                409, "Ein registrierter Sync-Unterprozess ist noch aktiv"
            )
        job_id = database.job_start(JOB_KIND, trigger="manual", exclusive_scope=True)
        run_id = runtime_state.begin_run(
            [str(pair.get("name") or "")], dry_run=False, kind=JOB_KIND, job_id=job_id
        )
    except JobAlreadyRunningError as exc:
        lease.release()
        raise HTTPException(
            409, "Während einer Sicherung oder Recovery kann kein weiterer Lauf starten"
        ) from exc
    except BaseException as exc:
        try:
            if database is not None and job_id is not None:
                _finish_failed_recovery(database, job_id, exc)
        finally:
            lease.release()
        raise
    return database, job_id, lease, run_id


def _queue_recovery(background, function, config, pair, *args, **kwargs) -> int:
    database, job_id, lease, run_id = _start_recovery_job(pair)
    try:
        if function is run_selective_restore:
            database.audit_add(
                "selective_recovery_started",
                actor="web",
                details={
                    "job_id": job_id,
                    "pair": str(pair.get("name") or ""),
                    "items": len(args[0]),
                    "recovery_point": kwargs["recovery_point"],
                },
            )
        background.add_task(
            _run_recovery_locked,
            lease,
            run_id,
            function,
            database,
            config,
            pair,
            *args,
            job_id=job_id,
            **kwargs,
        )
    except BaseException as exc:
        try:
            _finish_failed_recovery(database, job_id, exc)
        finally:
            _release_recovery(lease, run_id, "error")
        raise
    return job_id


@router.post("/snapshots", status_code=202)
def start_snapshot(
    body: SnapshotRequest, background: BackgroundTasks
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, body.identity)
    job_id = _queue_recovery(
        background,
        run_snapshot_capture,
        config,
        pair,
        max_total_mb=body.max_total_mb,
    )
    return {"ok": True, "job_id": job_id, "status": "running", "operation": "snapshot"}


@router.post("/points/{point_id}/restore", status_code=202)
def restore_full_snapshot(
    point_id: str, body: SnapshotRequest, background: BackgroundTasks
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, body.identity)
    try:
        load_manifest(config, pair, point_id)
    except SnapshotError as exc:
        raise HTTPException(400, str(exc)) from exc
    job_id = _queue_recovery(
        background,
        run_snapshot_restore,
        config,
        pair,
        point_id=point_id,
        max_total_mb=body.max_total_mb,
    )
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "operation": "full-restore",
    }


def _relative_path(value: str) -> str:
    original = str(value or "").replace("\\", "/").strip()
    if original.startswith("/"):
        raise HTTPException(400, "Ungültiger relativer Pfad")
    raw = original.strip("/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
        or ":" in raw
        or any(char in raw for char in ("\x00", "\r", "\n"))
    ):
        raise HTTPException(400, "Ungültiger relativer Pfad")
    return "" if raw in {"", "."} else path.as_posix()


@router.get("/browse")
def browse_backup(
    identity: str, path: str = Query("", max_length=1024)
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    _source, backup_target = _endpoints(pair)
    relative = _relative_path(path)
    truncated = False
    if _is_remote(backup_target):
        target = backup_target.rstrip("/") + (f"/{relative}" if relative else "")
        result = subprocess.run(
            ["rclone", "lsjson", "--max-depth", "1", "--", target],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode != 0:
            raise HTTPException(502, "Sicherungsziel konnte nicht gelesen werden")
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise HTTPException(
                502, "Sicherungsziel hat eine ungültige Antwort geliefert"
            ) from exc
        if not isinstance(entries, list):
            raise HTTPException(
                502, "Sicherungsziel hat eine ungültige Antwort geliefert"
            )
        truncated = len(entries) > 500
        items = [
            {
                "name": str(item.get("Name") or ""),
                "path": (f"{relative}/" if relative else "")
                + str(item.get("Name") or ""),
                "is_dir": bool(item.get("IsDir")),
                "size": item.get("Size"),
                "modified_at": item.get("ModTime"),
            }
            for item in entries[:500]
            if isinstance(item, Mapping)
            and str(item.get("Name") or "") not in {"", ".", ".."}
        ]
    else:
        try:
            items, truncated = _local_browse(backup_target, relative)
        except RecoveryPointError as exc:
            status = 404 if "nicht gefunden" in str(exc).lower() else 400
            raise HTTPException(status, str(exc)) from exc
    return {
        "pair": str(pair.get("name") or ""),
        "path": relative,
        "items": items,
        "truncated": truncated,
    }


@router.get("/points")
def recovery_points(
    identity: str = Query(min_length=1, max_length=128),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        points = list_points(config, pair)
    except RecoveryPointError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"pair": str(pair.get("name") or ""), "points": points}


@router.get("/points/{point_id}/browse")
def browse_recovery_point(
    point_id: str,
    identity: str = Query(min_length=1, max_length=128),
    path: str = Query(default="", max_length=1024),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        result = browse_point(config, pair, point_id, path)
    except RecoveryPointError as exc:
        message = str(exc)
        status = 404 if "nicht gefunden" in message.lower() else 502
        raise HTTPException(status, message) from exc
    return {"pair": str(pair.get("name") or ""), **result}


@router.get("/diff")
def recovery_diff(
    identity: str = Query(min_length=1, max_length=128),
    from_point: str = Query(min_length=1, max_length=128),
    to_point: str = Query(default="current", min_length=1, max_length=128),
) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, identity)
    try:
        result = compare_points(config, pair, from_point, to_point)
    except RecoveryPointError as exc:
        message = str(exc)
        status = 404 if "nicht gefunden" in message.lower() else 502
        raise HTTPException(status, message) from exc
    return {"pair": str(pair.get("name") or ""), **result}


class SelectiveRestoreRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=128)
    paths: list[str] = Field(min_length=1, max_length=100)
    max_total_mb: int = Field(default=512, ge=1, le=51_200)
    point_id: str = Field(default="current", min_length=1, max_length=128)


@router.post("/restore", status_code=202)
def start_selective_restore(
    body: SelectiveRestoreRequest, background: BackgroundTasks
) -> dict[str, Any]:
    selection = normalize_selection(body.paths)
    config = get_config().snapshot()
    pair = _find_pair(config, body.identity)
    try:
        recovery_source = point_target(config, pair, body.point_id)
    except RecoveryPointError as exc:
        raise HTTPException(404, str(exc)) from exc
    job_id = _queue_recovery(
        background,
        run_selective_restore,
        config,
        pair,
        selection,
        max_total_mb=body.max_total_mb,
        source_override=recovery_source,
        recovery_point=body.point_id,
    )
    return {"ok": True, "job_id": job_id, "status": "running"}


@router.get("/staging")
def staging_items() -> dict[str, Any]:
    return {"items": list_staging(get_db(), get_config().snapshot())}


class RemoveStagingRequest(ReauthenticationBody):
    pass


@router.delete("/staging/{recovery_id}")
def delete_staging(
    recovery_id: str,
    body: RemoveStagingRequest,
    request: Request,
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    require_reauthentication(request, user, body.current_password)
    try:
        removed = remove_staging(get_db(), get_config().snapshot(), recovery_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not removed:
        raise HTTPException(404, "Recovery-Staging nicht gefunden")
    return {"ok": True}


__all__ = ["build_recovery_pass", "encrypted_handover", "router"]
