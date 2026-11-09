"""API: Rotierender Cloud-Bitrot-Scrub."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs import bitrot_scrub
from ..jobs.scheduler import rclone_history_key
from ..lockdown import is_locked_down
from ..security import require_csrf
from .api_jobs import (
    _audit_best_effort,
    _finish_external_result,
    _finish_status,
    _known_pair_names,
    _locks,
    _remove_job_logger,
    _reserve_backup_job,
    _setup_job_logger,
    _start_thread,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/jobs/bitrot-scrub",
    tags=["jobs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class BitrotScrubPayload(BaseModel):
    pairs: Optional[list[str]] = None


# Pfad "" statt "/": Sonst fängt GET /api/jobs/{job_id} das POST ohne
# Schrägstrich als 405 ab.
@router.post("")
def start_bitrot_scrub(
    payload: Optional[BitrotScrubPayload] = None, user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Startet den Scrub im Backup-Scope (409, solange ein Backup/Drill läuft)."""
    if is_locked_down():
        raise HTTPException(423, "Lockdown aktiv")
    pairs_filter = (
        list(dict.fromkeys(payload.pairs)) if payload and payload.pairs else None
    )
    known = _known_pair_names()
    unknown = [name for name in (pairs_filter or []) if name not in known]
    if unknown:
        raise HTTPException(404, f"Pair nicht gefunden: {', '.join(unknown)}")
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup, Check oder Prüflauf läuft bereits")
    job_id, scope_lock = _reserve_backup_job(
        bitrot_scrub.JOB_KIND, config_revision=config_revision
    )
    _audit_best_effort(
        "bitrot_scrub_requested", actor=user, details={"pairs": pairs_filter or []}
    )

    def run_scrub() -> None:
        handler: logging.FileHandler | None = None
        worker_error: str | None = None
        finished = False
        try:
            db = get_db()
            if (db.job_get(job_id) or {}).get("status") != "running":
                return
            log_file, handler = _setup_job_logger(job_id, bitrot_scrub.JOB_KIND)
            db.job_set_log_file(job_id, str(log_file))
            result = bitrot_scrub.run_bitrot_scrub(
                pairs_filter,
                trigger="web",
                reset_cancel_state=False,
                config_snapshot=config_snapshot,
            )
            finished = True
            _finish_external_result(db, job_id, _finish_status(result), result)
        except Exception as exc:
            if not finished:
                worker_error = str(exc) or type(exc).__name__
            logger.exception("Bitrot-Scrub #%s fehlgeschlagen", job_id)
        finally:
            if worker_error:
                try:
                    get_db().job_finish(job_id, "error", {"error": worker_error})
                except Exception:
                    logger.exception(
                        "Scrub-Fehlerstatus konnte nicht gespeichert werden"
                    )
            _remove_job_logger(handler)
            scope_lock.release()
            _locks["backup"].release()

    _start_thread(
        run_scrub,
        job_id=job_id,
        name=f"bitrot-scrub-job-{job_id}",
        scope_lock=scope_lock,
    )
    return {"ok": True, "job_id": job_id}


@router.get("/status")
def bitrot_scrub_status() -> dict[str, Any]:
    cfg = get_config()
    db = get_db()
    jobs = db.job_list(kind=bitrot_scrub.JOB_KIND, limit=1)
    return {
        "last_job": jobs[0] if jobs else None,
        "due": bitrot_scrub.bitrot_scrub_due(cfg, db),
        "settings": bitrot_scrub.bitrot_scrub_settings(cfg),
    }


@router.get("/baseline")
def bitrot_baseline(
    pair: str = Query(..., min_length=1, max_length=200),
) -> dict[str, Any]:
    pairs = get_config().get("backup", "pairs", default=[]) or []
    match = next(
        (p for p in pairs if isinstance(p, dict) and p.get("name") == pair), None
    )
    if match is None:
        raise HTTPException(404, "Pair nicht gefunden")
    return {"pair": pair, **get_db().hash_baseline_stats(rclone_history_key(match))}
