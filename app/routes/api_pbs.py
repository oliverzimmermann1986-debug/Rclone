"""API: Proxmox-Backup-Server-Jobs (proxmox-backup-client)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..db import get_db
from ..jobs import pbs_backup
from ..jobs.locks import file_lock_or_none
from ..jobs.scheduler import next_run_after
from ..security import require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pbs",
    tags=["pbs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_lock = threading.Lock()


class PbsRunPayload(BaseModel):
    target: Optional[str] = Field(default=None, max_length=120)


@router.get("/status")
def pbs_status() -> dict[str, Any]:
    settings = pbs_backup.pbs_settings()
    db = get_db()
    running = db.job_running(pbs_backup.JOB_KIND)
    targets: list[dict[str, Any]] = []
    for target in pbs_backup.pbs_targets(settings):
        name = str(target.get("name"))
        last = db.pair_last_success(f"{pbs_backup.PAIR_PREFIX}{name}")
        schedule = str(target.get("schedule") or "manual")
        targets.append(
            {
                "name": name,
                "paths": target.get("paths") or [],
                "schedule": schedule,
                "namespace": target.get("namespace") or settings.get("namespace") or "",
                "last_success": (last or {}).get("ended_at"),
                "next_run": next_run_after(schedule),
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


def _run_thread(job_id: int, targets_filter: Optional[list[str]]) -> None:
    db = get_db()
    try:
        with file_lock_or_none(pbs_backup.JOB_KIND) as got_lock:
            if got_lock is None:
                db.job_finish(
                    job_id,
                    "skipped",
                    {"ok": False, "skipped": True, "error": "PBS-Job läuft bereits"},
                )
                return
            summary = pbs_backup.run_pbs_backup(targets_filter, trigger="web")
            db.job_finish(job_id, "ok" if summary.get("ok") else "error", summary)
    except Exception as exc:
        logger.exception("PBS-Job %s gescheitert", job_id)
        try:
            db.job_finish(job_id, "error", {"ok": False, "error": str(exc)})
        except Exception:
            logger.exception("PBS-Job %s konnte nicht abgeschlossen werden", job_id)
    finally:
        _lock.release()


@router.post("/run")
def pbs_run(payload: PbsRunPayload) -> dict[str, Any]:
    settings = pbs_backup.pbs_settings()
    if not bool(settings.get("enabled", False)):
        raise HTTPException(400, "PBS-Integration ist in den Einstellungen deaktiviert")
    if not pbs_backup.client_path():
        raise HTTPException(
            400,
            "proxmox-backup-client ist nicht installiert "
            "(apt install proxmox-backup-client)",
        )
    targets = {str(t.get("name")) for t in pbs_backup.pbs_targets(settings)}
    targets_filter: Optional[list[str]] = None
    if payload.target:
        if payload.target not in targets:
            raise HTTPException(404, f"Unbekanntes PBS-Target: {payload.target}")
        targets_filter = [payload.target]
    elif not targets:
        raise HTTPException(400, "Keine PBS-Targets konfiguriert")

    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "PBS-Backup läuft bereits")
    try:
        job_id = get_db().job_start(pbs_backup.JOB_KIND)
    except Exception as exc:
        _lock.release()
        logger.exception("PBS-Job konnte nicht angelegt werden")
        raise HTTPException(500, f"Job konnte nicht angelegt werden: {exc}")
    try:
        thread = threading.Thread(
            target=_run_thread,
            args=(job_id, targets_filter),
            name="pbs-backup",
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        _lock.release()
        get_db().job_finish(job_id, "error", {"ok": False, "error": str(exc)})
        raise HTTPException(500, f"PBS-Job konnte nicht gestartet werden: {exc}")
    return {"ok": True, "job_id": job_id, "targets": targets_filter or sorted(targets)}
