"""API: QEMU-Boot-Drill für PBS-VM-Images."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth
from ..config_store import get_config
from ..db import JobAlreadyRunningError, get_db
from ..jobs import boot_drill, pbs_backup
from ..jobs.job_lifecycle import PBS_KINDS, reconcile_locked_scope
from ..jobs.locks import HeldFileLock, try_file_lock
from ..lockdown import is_locked_down
from ..security import require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pbs/boot-drill",
    tags=["pbs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/status")
def boot_drill_status() -> dict[str, Any]:
    cfg = get_config()
    db = get_db()
    last = db.job_list(boot_drill.JOB_KIND, limit=1)
    return {
        "running": db.job_running(boot_drill.JOB_KIND),
        "last": last[0] if last else None,
        "due": boot_drill.boot_drill_due(cfg, db),
        "settings": boot_drill.boot_drill_settings(cfg),
    }


def _run_thread(
    job_id: int, scope_lock: HeldFileLock, config_snapshot: dict[str, Any]
) -> None:
    try:
        summary = boot_drill.run_boot_drill(
            trigger="web", config_snapshot=config_snapshot
        )
        db = get_db()
        if summary.get("log_file"):
            db.job_set_log_file(job_id, str(summary["log_file"]))
        finish = getattr(db, "job_finish_external", db.job_finish)
        finish(job_id, "ok" if summary.get("ok") else "error", summary)
    except Exception as exc:
        logger.exception("Boot-Drill-Job %s gescheitert", job_id)
        try:
            get_db().job_finish(job_id, "error", {"ok": False, "error": str(exc)})
        except Exception:
            logger.exception("Boot-Drill-Job %s nicht abschließbar", job_id)
    finally:
        scope_lock.release()


@router.post("")
def boot_drill_run() -> dict[str, Any]:
    if is_locked_down():
        raise HTTPException(423, "Lockdown aktiv")
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    # Gleicher Prozess-Lock wie PBS-Backups: Drill und Backup schließen sich aus.
    scope_lock = try_file_lock(pbs_backup.PBS_CANCEL_SCOPE)
    if scope_lock is None:
        raise HTTPException(409, "Ein PBS-Backup oder Boot-Drill läuft bereits")
    try:
        db = get_db()
        if not reconcile_locked_scope(
            db,
            scope=pbs_backup.PBS_CANCEL_SCOPE,
            kinds=(*PBS_KINDS, boot_drill.JOB_KIND),
        ).get("safe"):
            raise HTTPException(
                409, "Ein registrierter PBS-Unterprozess ist noch aktiv"
            )
        if is_locked_down():
            raise HTTPException(423, "Lockdown aktiv")
        job_id = db.job_start(
            boot_drill.JOB_KIND,
            attempts=[
                {
                    "name": boot_drill.RUN_NAME,
                    "history_key": boot_drill.BOOT_DRILL_HISTORY_KEY,
                    "trigger": "web",
                }
            ],
            exclusive_scope=True,
            config_revision=config_revision,
        )
    except HTTPException:
        scope_lock.release()
        raise
    except JobAlreadyRunningError as exc:
        scope_lock.release()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        scope_lock.release()
        logger.exception("Boot-Drill-Job konnte nicht angelegt werden")
        raise HTTPException(500, f"Job konnte nicht angelegt werden: {exc}") from exc
    try:
        threading.Thread(
            target=_run_thread,
            args=(job_id, scope_lock, config_snapshot),
            name="pbs-boot-drill",
            daemon=True,
        ).start()
    except Exception as exc:
        scope_lock.release()
        try:
            get_db().job_finish(job_id, "error", {"ok": False, "error": str(exc)})
        except Exception:
            logger.exception("Boot-Drill-Startfehler #%s nicht speicherbar", job_id)
        raise HTTPException(500, f"Boot-Drill konnte nicht starten: {exc}") from exc
    try:
        db.audit_add("boot_drill_requested", actor="web", details={"job_id": job_id})
    except Exception:
        logger.exception("Audit für Boot-Drill #%s fehlgeschlagen", job_id)
    return {"job_id": job_id}
