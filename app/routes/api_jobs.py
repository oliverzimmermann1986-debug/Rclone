"""API für Sync-Jobs: Start, Cancel, Progress, History, Logs."""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs import rclone_sync as rclone_job
from ..jobs.locks import file_lock_or_none

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_auth)])

_locks: Dict[str, threading.Lock] = {"backup": threading.Lock()}


def _setup_job_logger(job_id: int, kind: str):
    log_dir = Path(get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-job{job_id}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    return log_file, fh


def _run_backup_thread(job_id: int, dry_run: bool, pairs_filter=None):
    db = get_db()
    fh = None
    try:
        with file_lock_or_none("backup") as flock:
            if flock is None:
                db.job_finish(job_id, "skipped", {"error": "anderer Backup läuft (CLI?)"})
                return
            try:
                log_file, fh = _setup_job_logger(job_id, "backup")
                db.job_set_log_file(job_id, str(log_file))
                logger.info(f"=== Backup #{job_id} startet (dry_run={dry_run}, pairs={pairs_filter}) ===")
                summary = rclone_job.run_job(dry_run=dry_run, pairs_filter=pairs_filter)
                if rclone_job.is_cancelled():
                    status = "error"
                    summary["error"] = "Abgebrochen"
                elif summary.get("enabled") is False:
                    status = "ok"
                elif "ok" in summary:
                    status = "ok" if summary["ok"] else "error"
                else:
                    status = "ok" if summary.get("ok_count", 0) == summary.get("total_pairs", 0) else "error"
                db.job_finish(job_id, status, summary)
                logger.info(f"=== Backup #{job_id} {status} ===")
            except Exception as e:
                logger.exception(f"Backup #{job_id} fehlgeschlagen")
                db.job_finish(job_id, "error", {"error": str(e)})
    except Exception as e:
        try:
            db.job_finish(job_id, "error", {"error": f"setup failed: {e}"})
        except Exception:
            pass
    finally:
        if fh is not None:
            try:
                logging.getLogger().removeHandler(fh)
                fh.close()
            except Exception:
                pass
        _locks["backup"].release()


@router.post("/backup/run")
def run_backup(dry_run: bool = Query(False), pairs: Optional[str] = Query(None)):
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    pairs_filter = [p.strip() for p in pairs.split(",")] if pairs else None
    job_id = get_db().job_start("backup")
    t = threading.Thread(target=_run_backup_thread, args=(job_id, dry_run, pairs_filter), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id, "pairs": pairs_filter}


@router.post("/backup/cancel")
def cancel_backup():
    db = get_db()
    if not db.job_running("backup"):
        return {"ok": False, "error": "Kein laufender Backup"}
    return rclone_job.cancel_job()


@router.post("/backup/run-pair/{pair_name}")
def run_single_pair(pair_name: str):
    """Einzelnes Pair triggern (für Audit / On-Demand-Sync)."""
    return run_backup(dry_run=False, pairs=pair_name)


@router.get("/backup/progress")
def backup_progress():
    """Live-Progress mit per-Pair-Stats aus dem Log."""
    db = get_db()
    cfg = get_config()
    running = db.job_running("backup")
    if not running:
        last = db.job_list(kind="backup", limit=1)
        return {"running": False, "last": last[0] if last else None}

    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    started = float(running["started_at"])

    pairs_cfg = cfg.get("backup", "pairs", default=[]) or []
    pairs_status = []
    log_file = running.get("log_file")
    log_text = ""
    if log_file and Path(log_file).exists():
        try:
            with open(log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 32768))
                log_text = f.read().decode("utf-8", errors="ignore")
        except Exception:
            pass

    stats_re = re.compile(
        r'Transferred:\s*([\d.]+\s*\w*)\s*/\s*([\d.]+\s*\w*)'
        r'(?:,\s*(?:([\d.]+)\s*%|-))?'
        r'(?:,\s*([\d.]+\s*\w*)/s)?'
        r'(?:,\s*ETA\s*(\S+))?'
    )
    for pair in pairs_cfg:
        name = pair.get("name", "")
        status = "pending"
        if f"PAAR '{name}'" in log_text or f"pair '{name}'" in log_text.lower():
            status = "running"
        if f"PAAR '{name}' OK" in log_text or f"'{name}' fertig" in log_text:
            status = "done"
        if f"'{name}' FEHLER" in log_text or f"'{name}' ERROR" in log_text:
            status = "error"
        # Letzte Stats-Zeile für dieses Pair
        pair_block = log_text.split(f"PAAR '{name}'")
        latest_stats = None
        if len(pair_block) > 1:
            for line in reversed(pair_block[-1].splitlines()):
                m = stats_re.search(line)
                if m:
                    latest_stats = m
                    break
        if latest_stats:
            pairs_status.append({
                "name": name,
                "status": status,
                "transferred": latest_stats.group(1),
                "total": latest_stats.group(2),
                "percent": float(latest_stats.group(3)) if latest_stats.group(3) else None,
                "speed": latest_stats.group(4),
                "eta": latest_stats.group(5),
            })
        else:
            pairs_status.append({"name": name, "status": status, "transferred": None,
                                  "total": None, "percent": None, "speed": None, "eta": None})

    return {
        "running": True,
        "job_id": running["id"],
        "started_at": started,
        "elapsed_sec": round(time.time() - started),
        "pairs": pairs_status,
        "total_pairs": len(pairs_cfg),
        "done_pairs": sum(1 for p in pairs_status if p["status"] == "done"),
    }



@router.post("/backup/quick")
def run_quick_sync(payload: dict):
    """Ad-hoc-Sync ohne Pair-Speichern. Payload: {remote, local, direction,
    mode, dry_run, extra_args}. direction=pull|push|bisync, mode=copy|sync|bisync."""
    from ..jobs import rclone_sync as rj
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    job_id = get_db().job_start("quicksync")
    def _run():
        db = get_db()
        fh = None
        try:
            with file_lock_or_none("backup") as flock:
                if flock is None:
                    db.job_finish(job_id, "skipped", {"error": "anderer Backup läuft"})
                    return
                try:
                    log_file, fh = _setup_job_logger(job_id, "quicksync")
                    db.job_set_log_file(job_id, str(log_file))
                    result = rj.run_quick(
                        remote_path=payload.get("remote", ""),
                        local_path=payload.get("local", ""),
                        direction=payload.get("direction", "bisync"),
                        mode=payload.get("mode", "bisync"),
                        dry_run=bool(payload.get("dry_run", False)),
                        extra_args=payload.get("extra_args"),
                    )
                    status = "ok" if result.get("ok") else "error"
                    db.job_finish(job_id, status, result)
                except Exception as e:
                    logger.exception(f"QuickSync #{job_id} fail")
                    db.job_finish(job_id, "error", {"error": str(e)})
        finally:
            if fh:
                try:
                    logging.getLogger().removeHandler(fh)
                    fh.close()
                except Exception: pass
            _locks["backup"].release()
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@router.get("/list")
def list_jobs(kind: Optional[str] = None, limit: int = 50):
    return get_db().job_list(kind=kind, limit=limit)


@router.post("/cleanup-failed")
def cleanup_failed_jobs():
    deleted = get_db().jobs_delete_failed()
    return {"ok": True, "deleted": deleted}


@router.get("/{job_id}")
def job_detail(job_id: int):
    j = get_db().job_get(job_id)
    if not j:
        raise HTTPException(404, "Nicht gefunden")
    return j


@router.get("/{job_id}/log")
def job_log(job_id: int, tail: int = 500):
    j = get_db().job_get(job_id)
    if not j:
        raise HTTPException(404, "Job nicht gefunden")
    log_file = j.get("log_file")
    if not log_file or not Path(log_file).exists():
        return {"log": ""}
    try:
        with open(log_file, "r", errors="ignore") as f:
            lines = f.readlines()[-tail:]
        return {"log": "".join(lines)}
    except Exception as e:
        return {"log": f"<Fehler: {e}>"}


@router.get("/status/current")
def status_current():
    db = get_db()
    return {"backup": db.job_running("backup")}
