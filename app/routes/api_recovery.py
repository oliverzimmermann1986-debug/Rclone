"""Recovery Center: evidence, quarantine and isolated selective restores."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path, PurePosixPath
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
    browse_point,
    compare_points,
    list_points,
    point_target,
)
from ..secret_redaction import REDACTED, redact_secrets
from ..security import require_csrf
from . import api_diagnostics, api_storage

router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


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
    config = redact_secrets(get_config().snapshot())
    package = {
        "created_at": time.time(),
        "recovery_pass": build_recovery_pass(include_paths=body.include_paths),
        "config": config,
        "instructions": [
            "Dieses Paket enthält keine Anmeldedaten oder Cloud-Schlüssel.",
            "Passphrase getrennt und sicher an die bevollmächtigte Person übermitteln.",
            "Wiederherstellungen zuerst in einen getrennten Staging-Ordner durchführen.",
        ],
    }
    envelope = encrypted_handover(package, body.passphrase)
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


def _relative_path(value: str) -> str:
    original = str(value or "").replace("\\", "/").strip()
    if original.startswith("/"):
        raise HTTPException(400, "Ungültiger relativer Pfad")
    raw = original.strip("/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
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
    if ":" in backup_target.split("/", 1)[0]:
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
        items = [
            {
                "name": str(item.get("Name") or ""),
                "path": (f"{relative}/" if relative else "")
                + str(item.get("Name") or ""),
                "is_dir": bool(item.get("IsDir")),
                "size": item.get("Size"),
                "modified_at": item.get("ModTime"),
            }
            for item in entries
            if isinstance(item, Mapping)
            and str(item.get("Name") or "") not in {".", ".."}
        ]
    else:
        root = Path(backup_target).expanduser().resolve()
        target = (root / relative).resolve()
        if target != root and not target.is_relative_to(root):
            raise HTTPException(400, "Pfad liegt außerhalb des Sicherungsziels")
        if not target.is_dir():
            raise HTTPException(404, "Ordner nicht gefunden")
        items = []
        for item in sorted(
            target.iterdir(),
            key=lambda entry: (not entry.is_dir(), entry.name.casefold()),
        )[:500]:
            stat = item.stat()
            items.append(
                {
                    "name": item.name,
                    "path": (f"{relative}/" if relative else "") + item.name,
                    "is_dir": item.is_dir(),
                    "size": stat.st_size if item.is_file() else None,
                    "modified_at": stat.st_mtime,
                }
            )
    return {"pair": str(pair.get("name") or ""), "path": relative, "items": items}


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
    database = get_db()
    try:
        job_id = database.job_start(JOB_KIND, trigger="manual", exclusive_scope=True)
    except JobAlreadyRunningError as exc:
        raise HTTPException(
            409,
            "Während einer Sicherung, Prüfung oder anderen Recovery kann keine "
            "selektive Wiederherstellung starten",
        ) from exc
    database.audit_add(
        "selective_recovery_started",
        actor="web",
        details={
            "job_id": job_id,
            "pair": str(pair.get("name") or ""),
            "items": len(selection),
            "recovery_point": body.point_id,
        },
    )
    background.add_task(
        run_selective_restore,
        database,
        config,
        pair,
        selection,
        max_total_mb=body.max_total_mb,
        job_id=job_id,
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
