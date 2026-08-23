"""API: Proxmox-Backup-Server-Jobs (proxmox-backup-client)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import JobAlreadyRunningError, get_db
from ..jobs import pbs_backup, rclone_sync, runtime_state
from ..jobs.job_lifecycle import PBS_KINDS, reconcile_locked_scope
from ..jobs.locks import HeldFileLock, try_file_lock
from ..jobs.scheduler import next_run_after, pbs_history_key
from ..security import require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pbs",
    tags=["pbs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_lock = threading.Lock()


def _audit_best_effort(event: str, details: dict[str, Any]) -> None:
    try:
        get_db().audit_add(event, actor="web", details=details)
    except Exception:
        logger.exception("Audit-Ereignis %s konnte nicht gespeichert werden", event)


def _audit_prune_failures(summary: dict[str, Any], *, job_id: int) -> None:
    failures = pbs_backup.prune_failures(summary)
    if not failures:
        return
    _audit_best_effort(
        "pbs_prune_failed",
        {
            "job_id": job_id,
            "trigger": str(summary.get("trigger") or "web"),
            "backup_ok": summary.get("backup_ok") is True,
            "maintenance_failed": True,
            "targets": failures,
        },
    )


class PbsRunPayload(BaseModel):
    target: Optional[str] = Field(default=None, max_length=120)


@router.get("/status")
def pbs_status() -> dict[str, Any]:
    cfg = get_config()
    settings = pbs_backup.pbs_settings()
    db = get_db()
    running = db.job_running(pbs_backup.JOB_KIND)
    timezone_name = str(
        cfg.get("backup", "timezone", default="Europe/Berlin") or "Europe/Berlin"
    )
    targets: list[dict[str, Any]] = []
    for target in pbs_backup.pbs_targets(settings):
        name = str(target.get("name"))
        last = db.pair_last_success(
            f"{pbs_backup.PAIR_PREFIX}{name}",
            history_key=pbs_history_key(settings, target),
        )
        last_pair = (last or {}).get("pair") or {}
        if not isinstance(last_pair, dict):
            last_pair = {}
        schedule = str(target.get("schedule") or "manual")
        targets.append(
            {
                "name": name,
                "paths": target.get("paths") or [],
                "schedule": schedule,
                "namespace": target.get("namespace") or settings.get("namespace") or "",
                "last_success": (last or {}).get("ended_at"),
                "last_backup_ok": (
                    last_pair.get("backup_ok", True) is True if last else None
                ),
                "last_prune_ok": last_pair.get("prune_ok"),
                "last_prune_error": last_pair.get("prune_error"),
                "maintenance_failed": last_pair.get("maintenance_failed") is True,
                "next_run": next_run_after(
                    schedule,
                    timezone_name=timezone_name,
                ),
            }
        )
    repository = str(settings.get("repository") or "")
    return {
        "enabled": bool(settings.get("enabled", False)),
        "client_available": pbs_backup.client_path() is not None,
        "repository": repository,
        "namespace": settings.get("namespace") or "",
        "running": bool(running),
        "running_job": running,
        "targets": targets,
    }


def _run_thread(
    job_id: int,
    targets_filter: Optional[list[str]],
    history_keys: dict[str, str],
    scope_lock: HeldFileLock,
    config_snapshot: dict[str, Any],
) -> None:
    db = None
    worker_error: str | None = None
    try:
        db = get_db()
        current = db.job_get(job_id) or {}
        if current.get("status") != "running":
            return
        summary = pbs_backup.run_pbs_backup(
            targets_filter,
            trigger="web",
            reset_cancel_state=False,
            config_snapshot=config_snapshot,
        )
        summary["history_keys"] = history_keys
        status = (
            "cancelled"
            if summary.get("cancelled")
            else ("ok" if summary.get("ok") else "error")
        )
        db.job_finish(job_id, status, summary)
        _audit_prune_failures(summary, job_id=job_id)
    except Exception as exc:
        worker_error = str(exc)
        logger.exception("PBS-Job %s gescheitert", job_id)
    finally:
        final_db = db
        if final_db is None:
            try:
                final_db = get_db()
            except Exception:
                logger.exception("PBS-DB konnte beim Abschluss nicht geöffnet werden")
        if final_db is not None and worker_error:
            try:
                final_db.job_finish(
                    job_id,
                    "error",
                    {"ok": False, "error": worker_error},
                )
            except Exception:
                logger.exception("PBS-Job %s konnte nicht abgeschlossen werden", job_id)
        scope_lock.release()
        _lock.release()


@router.post("/run")
def pbs_run(payload: PbsRunPayload) -> dict[str, Any]:
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    settings = pbs_backup.pbs_settings(rclone_sync._SnapshotConfig(config_snapshot))
    if not bool(settings.get("enabled", False)):
        raise HTTPException(400, "PBS-Integration ist in den Einstellungen deaktiviert")
    if not pbs_backup.client_path():
        raise HTTPException(
            400,
            "proxmox-backup-client ist nicht installiert "
            "(apt install proxmox-backup-client)",
        )
    configured_targets = pbs_backup.pbs_targets(settings)
    targets = {str(t.get("name")) for t in configured_targets}
    targets_filter: Optional[list[str]] = None
    if payload.target:
        if payload.target not in targets:
            raise HTTPException(404, f"Unbekanntes PBS-Target: {payload.target}")
        targets_filter = [payload.target]
    elif not targets:
        raise HTTPException(400, "Keine PBS-Targets konfiguriert")

    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "PBS-Backup läuft bereits")
    selected_targets = [
        target
        for target in configured_targets
        if not targets_filter or str(target.get("name")) in targets_filter
    ]
    history_keys = {
        f"{pbs_backup.PAIR_PREFIX}{target.get('name')}": pbs_history_key(
            settings, target
        )
        for target in selected_targets
    }
    attempts = [
        {
            "name": name,
            "history_key": history_key,
            "trigger": "web",
        }
        for name, history_key in history_keys.items()
    ]
    scope_lock: HeldFileLock | None = None
    try:
        scope_lock = try_file_lock(pbs_backup.PBS_CANCEL_SCOPE)
        if scope_lock is None:
            raise HTTPException(409, "Ein anderer PBS-Job hält den Prozess-Lock")
        db = get_db()
        reconciliation = reconcile_locked_scope(
            db,
            scope=pbs_backup.PBS_CANCEL_SCOPE,
            kinds=PBS_KINDS,
        )
        if not reconciliation.get("safe"):
            raise HTTPException(
                409,
                "Ein registrierter PBS-Unterprozess ist noch aktiv",
            )
        rclone_sync.reset_cancel(pbs_backup.PBS_CANCEL_SCOPE)
        job_id = db.job_start(
            pbs_backup.JOB_KIND,
            attempts=attempts,
            exclusive_scope=True,
            config_revision=config_revision,
        )
    except HTTPException:
        if scope_lock is not None:
            scope_lock.release()
        _lock.release()
        raise
    except JobAlreadyRunningError as exc:
        if scope_lock is not None:
            scope_lock.release()
        _lock.release()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        if scope_lock is not None:
            scope_lock.release()
        _lock.release()
        logger.exception("PBS-Job konnte nicht angelegt werden")
        raise HTTPException(500, f"Job konnte nicht angelegt werden: {exc}") from exc
    try:
        thread = threading.Thread(
            target=_run_thread,
            args=(
                job_id,
                targets_filter,
                history_keys,
                scope_lock,
                config_snapshot,
            ),
            name="pbs-backup",
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        scope_lock.release()
        _lock.release()
        try:
            get_db().job_finish(job_id, "error", {"ok": False, "error": str(exc)})
        except Exception:
            logger.exception("PBS-Thread-Startfehler konnte nicht gespeichert werden")
        raise HTTPException(
            500, f"PBS-Job konnte nicht gestartet werden: {exc}"
        ) from exc
    _audit_best_effort(
        "pbs_requested",
        {"job_id": job_id, "targets": targets_filter or sorted(targets)},
    )
    return {"ok": True, "job_id": job_id, "targets": targets_filter or sorted(targets)}


@router.post("/cancel")
def pbs_cancel(response: Response) -> dict[str, Any]:
    running = get_db().job_running(pbs_backup.JOB_KIND)
    if not running and not runtime_state.active_processes(pbs_backup.PBS_CANCEL_SCOPE):
        return {"ok": False, "error": "Kein laufender PBS-Job"}
    result = rclone_sync.cancel_job(pbs_backup.PBS_CANCEL_SCOPE)
    _audit_best_effort(
        "pbs_cancel_requested",
        {
            "job_id": (running or {}).get("id"),
            "ok": result.get("ok", False),
            "killed": result.get("killed", 0),
            "signal_persisted": result.get("signal_persisted"),
            "process_scan_ok": result.get("process_scan_ok"),
            "error_code": result.get("error_code"),
        },
    )
    if not result.get("ok"):
        response.status_code = 503
    return result
