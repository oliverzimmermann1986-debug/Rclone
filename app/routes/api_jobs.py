"""API für Sync-Jobs: Start, Cancel, Progress, History und Logs."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs import rclone_sync as rclone_job
from ..rclone_args import rclone_subprocess_env
from ..scheduler_control import pause_scheduler, resume_scheduler, scheduler_state
from ..jobs.locks import file_lock_or_none
from ..jobs.runtime_state import active_processes
from ..security import ensure_within, is_relative_to, parse_browse_roots, require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_locks: dict[str, threading.Lock] = {"backup": threading.Lock()}


def _job_log_target() -> logging.Logger:
    return logging.getLogger("app.jobs")


def _setup_job_logger(job_id: int, kind: str) -> tuple[Path, logging.FileHandler]:
    log_dir = Path(
        get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}-job{job_id}.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    try:
        os.chmod(log_file, 0o600)
    except OSError:
        pass
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    _job_log_target().addHandler(handler)
    return log_file, handler


def _remove_job_logger(handler: logging.FileHandler | None) -> None:
    if handler is None:
        return
    try:
        _job_log_target().removeHandler(handler)
        handler.close()
    except Exception:
        logger.exception("Job-Log-Handler konnte nicht geschlossen werden")


def _known_pair_names() -> set[str]:
    pairs = get_config().get("backup", "pairs", default=[]) or []
    return {
        str(pair.get("name"))
        for pair in pairs
        if isinstance(pair, dict) and pair.get("name")
    }


def _parse_pair_filter(raw: Optional[str]) -> Optional[list[str]]:
    if raw is None:
        return None
    names = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not names:
        raise HTTPException(400, "Keine Pair-Namen angegeben")
    unknown = [name for name in names if name not in _known_pair_names()]
    if unknown:
        raise HTTPException(404, f"Pair(s) nicht gefunden: {', '.join(unknown)}")
    return names


def _finish_status(result: dict[str, Any]) -> str:
    if result.get("cancelled") or rclone_job.is_cancelled():
        return "cancelled"
    return "ok" if result.get("ok") else "error"


def _run_backup_thread(
    job_id: int, dry_run: bool, pairs_filter: Optional[list[str]]
) -> None:
    db = get_db()
    handler: logging.FileHandler | None = None
    try:
        with file_lock_or_none("backup") as file_lock:
            if file_lock is None:
                db.job_finish(
                    job_id,
                    "skipped",
                    {"error": "Ein anderer Sync hält den Prozess-Lock"},
                )
                return
            log_file, handler = _setup_job_logger(job_id, "backup")
            db.job_set_log_file(job_id, str(log_file))
            logger.info(
                "Backup #%s startet (dry_run=%s, pairs=%s)",
                job_id,
                dry_run,
                pairs_filter,
            )
            try:
                summary = rclone_job.run_job(
                    dry_run=dry_run, pairs_filter=pairs_filter, trigger="web"
                )
            except Exception as exc:
                logger.exception("Backup #%s fehlgeschlagen", job_id)
                db.job_finish(job_id, "error", {"error": str(exc)})
                return
            status = _finish_status(summary)
            if status == "cancelled":
                summary.setdefault("error", "Abgebrochen")
                summary["cancelled"] = True
            db.job_finish(job_id, status, summary)
            logger.info("Backup #%s %s", job_id, status)
    except Exception as exc:
        logger.exception("Backup #%s Setup fehlgeschlagen", job_id)
        try:
            db.job_finish(job_id, "error", {"error": f"Setup fehlgeschlagen: {exc}"})
        except Exception:
            logger.exception("Jobstatus konnte nicht gespeichert werden")
    finally:
        _remove_job_logger(handler)
        _locks["backup"].release()


def _start_job_or_release(kind: str, log_file: Optional[str] = None) -> int:
    """Legt den Job an; gibt bei Fehlern den bereits gehaltenen Lock wieder frei.

    Ohne diese Absicherung bleibt _locks["backup"] nach einer job_start-Exception
    (DB gesperrt, Platte voll) dauerhaft belegt und jeder weitere Sync liefert 409.
    """
    try:
        return get_db().job_start(kind, log_file=log_file)
    except Exception as exc:
        _locks["backup"].release()
        logger.exception("Job (%s) konnte nicht angelegt werden", kind)
        raise HTTPException(500, f"Job konnte nicht angelegt werden: {exc}")


def _audit_best_effort(event: str, *, actor: str, details: dict) -> None:
    """Audit darf einen bereits angelegten Job nicht verhindern."""
    try:
        get_db().audit_add(event, actor=actor, details=details)
    except Exception:
        logger.exception("Audit-Event %s konnte nicht gespeichert werden", event)


def _start_thread(
    target, *, job_id: int, name: str, args: tuple[Any, ...] = ()
) -> None:
    try:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
    except Exception as exc:
        _locks["backup"].release()
        get_db().job_finish(
            job_id, "error", {"error": f"Thread konnte nicht gestartet werden: {exc}"}
        )
        raise HTTPException(500, "Job konnte nicht gestartet werden")


class SchedulerPausePayload(BaseModel):
    minutes: int | None = Field(default=60, ge=1, le=44640)
    until: float | None = Field(default=None, gt=0)
    reason: str = Field(default="Wartungsfenster", max_length=300)


@router.get("/scheduler/state")
def get_scheduler_state() -> dict[str, Any]:
    cfg = get_config()
    state = scheduler_state(get_db())
    state["enabled"] = bool(cfg.get("backup", "enabled", default=True))
    state["timezone"] = str(
        cfg.get("backup", "timezone", default="Europe/Berlin") or "Europe/Berlin"
    )
    return state


@router.post("/scheduler/pause")
def pause_scheduler_endpoint(
    body: SchedulerPausePayload, user: str = Depends(require_auth)
) -> dict[str, Any]:
    try:
        return pause_scheduler(
            until=body.until,
            seconds=None if body.until is not None else int(body.minutes or 60) * 60,
            reason=body.reason,
            actor=user,
            db=get_db(),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/scheduler/resume")
def resume_scheduler_endpoint(user: str = Depends(require_auth)) -> dict[str, Any]:
    return resume_scheduler(actor=user, db=get_db())


@router.post("/backup/run")
def run_backup(
    dry_run: bool = Query(False), pairs: Optional[str] = Query(None)
) -> dict[str, Any]:
    pairs_filter = _parse_pair_filter(pairs)
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    job_id = _start_job_or_release("backup")
    _audit_best_effort(
        "backup_requested",
        actor="web",
        details={"dry_run": dry_run, "pairs": pairs_filter or []},
    )
    _start_thread(
        _run_backup_thread,
        job_id=job_id,
        name=f"backup-job-{job_id}",
        args=(job_id, dry_run, pairs_filter),
    )
    return {"ok": True, "job_id": job_id, "pairs": pairs_filter}


@router.get("/backup/plan")
def backup_plan(
    dry_run: bool = Query(True), pairs: Optional[str] = Query(None)
) -> dict[str, Any]:
    return rclone_job.build_job_plan(
        dry_run=dry_run, pairs_filter=_parse_pair_filter(pairs)
    )


@router.post("/backup/cancel")
def cancel_backup() -> dict[str, Any]:
    db = get_db()
    running = (
        db.job_running("backup")
        or db.job_running("check")
        or db.job_running("quicksync")
    )
    state = rclone_job.get_runtime_state() or {}
    if not running and state.get("status") != "running" and not active_processes():
        return {"ok": False, "error": "Kein laufender Job"}
    return rclone_job.cancel_job()


@router.post("/backup/run-pair/{pair_name}")
def run_single_pair(pair_name: str, dry_run: bool = Query(False)) -> dict[str, Any]:
    if pair_name not in _known_pair_names():
        raise HTTPException(404, "Pair nicht gefunden")
    return run_backup(dry_run=dry_run, pairs=pair_name)


@router.post("/backup/check/{pair_name}")
def check_pair(
    pair_name: str,
    one_way: Optional[bool] = Query(None),
    download: bool = Query(False),
) -> dict[str, Any]:
    if pair_name not in _known_pair_names():
        raise HTTPException(404, "Pair nicht gefunden")
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup/Check läuft bereits")
    job_id = _start_job_or_release("check")
    _audit_best_effort(
        "check_requested",
        actor="web",
        details={"pair": pair_name, "one_way": one_way, "download": download},
    )

    def run_check() -> None:
        db = get_db()
        handler: logging.FileHandler | None = None
        try:
            with file_lock_or_none("backup") as file_lock:
                if file_lock is None:
                    db.job_finish(
                        job_id,
                        "skipped",
                        {"error": "Ein anderer Sync hält den Prozess-Lock"},
                    )
                    return
                log_file, handler = _setup_job_logger(job_id, "check")
                db.job_set_log_file(job_id, str(log_file))
                result = rclone_job.run_pair_check(
                    pair_name, one_way=one_way, download=download
                )
                db.job_finish(job_id, _finish_status(result), result)
        except Exception as exc:
            logger.exception("Check #%s fehlgeschlagen", job_id)
            db.job_finish(job_id, "error", {"error": str(exc)})
        finally:
            _remove_job_logger(handler)
            _locks["backup"].release()

    _start_thread(run_check, job_id=job_id, name=f"check-job-{job_id}")
    return {"ok": True, "job_id": job_id}


def _latest_stats(text: str) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "transferred": None,
        "total": None,
        "percent": None,
        "speed": None,
        "eta": None,
    }
    stats.update(rclone_job.parse_final_stats(text))
    return stats


@router.get("/backup/progress")
def backup_progress() -> dict[str, Any]:
    db = get_db()
    running = db.job_running("backup")
    state = rclone_job.get_runtime_state() or {}
    if not running or state.get("status") != "running":
        last = db.job_list(kind="backup", limit=1)
        return {"running": False, "last": last[0] if last else None}

    started = float(state.get("started_at") or running["started_at"])
    pairs_status: list[dict[str, Any]] = []
    for name, pair_state in (state.get("pairs") or {}).items():
        if not isinstance(pair_state, dict):
            continue
        log_file = pair_state.get("log_file")
        text = rclone_job.read_log_tail(Path(log_file)) if log_file else ""
        item = {
            "name": str(name),
            "status": pair_state.get("status", "pending"),
            "log_file": log_file,
            "error": pair_state.get("error"),
            **_latest_stats(text),
        }
        pairs_status.append(item)
    pairs_status.sort(key=lambda item: str(item["name"]).casefold())
    finished = {"done", "error", "cancelled", "skipped"}
    return {
        "running": True,
        "job_id": running["id"],
        "started_at": started,
        "elapsed_sec": max(0, round(time.time() - started)),
        "pairs": pairs_status,
        "total_pairs": len(pairs_status),
        "done_pairs": sum(1 for pair in pairs_status if pair["status"] in finished),
    }


class QuickSyncPayload(BaseModel):
    remote: str = Field(min_length=3, max_length=4096)
    local: str = Field(min_length=1, max_length=4096)
    direction: str = Field(default="bisync", pattern="^(pull|push|bisync)$")
    mode: str = Field(default="bisync", pattern="^(copy|sync|bisync)$")
    dry_run: bool = False
    extra_args: list[str] | str | None = None
    allow_delete: bool = False
    max_delete: int | None = Field(default=None, ge=0, le=10_000_000)
    min_local_files: int = Field(default=1, ge=0, le=1_000_000)


def _validate_quick_paths(payload: QuickSyncPayload) -> None:
    if any(char in payload.remote + payload.local for char in ("\x00", "\n", "\r")):
        raise HTTPException(400, "Pfad enthält ungültige Zeichen")
    remote_is_local = payload.remote.startswith("/")
    if not remote_is_local and (
        ":" not in payload.remote or payload.remote.startswith((":", "-"))
    ):
        raise HTTPException(
            400,
            "Remote muss ein konfigurierter rclone-Pfad oder ein absoluter "
            "lokaler Pfad sein",
        )
    if remote_is_local:
        roots_for_remote = parse_browse_roots(
            get_config().get(
                "web",
                "local_browse_roots",
                default=["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
            )
        )
        if not roots_for_remote:
            raise HTTPException(503, "Keine lokalen Quick-Sync-Wurzeln konfiguriert")
        ensure_within(Path(payload.remote), roots_for_remote)
        if Path(payload.remote.rstrip("/")) == Path(payload.local.rstrip("/")):
            raise HTTPException(400, "Quelle und Ziel sind identisch")
        return _finish_quick_validation(payload)
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, f"rclone-Remotes konnten nicht geprüft werden: {exc}")
    remotes = (
        {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if result.returncode == 0
        else set()
    )
    remote_name = payload.remote.split(":", 1)[0] + ":"
    if remote_name not in remotes:
        raise HTTPException(403, "Remote ist nicht konfiguriert")

    roots = parse_browse_roots(
        get_config().get(
            "web",
            "local_browse_roots",
            default=["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
        )
    )
    if not roots:
        raise HTTPException(503, "Keine lokalen Quick-Sync-Wurzeln konfiguriert")
    ensure_within(Path(payload.local), roots)
    _finish_quick_validation(payload)


def _finish_quick_validation(payload: QuickSyncPayload) -> None:
    if payload.direction == "bisync" and payload.mode != "bisync":
        raise HTTPException(400, "Bei direction=bisync muss mode=bisync sein")
    if payload.direction != "bisync" and payload.mode == "bisync":
        raise HTTPException(400, "Bei pull/push muss mode copy oder sync sein")
    if payload.mode in {"sync", "bisync"} and not payload.dry_run:
        backup = get_config().get("backup", default={}) or {}
        if backup.get("require_delete_confirmation", True) and not payload.allow_delete:
            raise HTTPException(
                400,
                "Produktiver Quick-Lauf benötigt eine ausdrückliche Löschbestätigung",
            )
        if (
            backup.get("require_max_delete_for_sync", True)
            and payload.max_delete is None
        ):
            raise HTTPException(
                400,
                "Produktiver Quick-Lauf benötigt eine begrenzte maximale Löschanzahl",
            )


@router.post("/backup/quick")
def run_quick_sync(payload: QuickSyncPayload) -> dict[str, Any]:
    _validate_quick_paths(payload)
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    job_id = _start_job_or_release("quicksync")
    _audit_best_effort(
        "quicksync_requested",
        actor="web",
        details={
            "direction": payload.direction,
            "mode": payload.mode,
            "dry_run": payload.dry_run,
        },
    )

    def run_quick() -> None:
        db = get_db()
        handler: logging.FileHandler | None = None
        try:
            with file_lock_or_none("backup") as file_lock:
                if file_lock is None:
                    db.job_finish(
                        job_id,
                        "skipped",
                        {"error": "Ein anderer Sync hält den Prozess-Lock"},
                    )
                    return
                log_file, handler = _setup_job_logger(job_id, "quicksync")
                db.job_set_log_file(job_id, str(log_file))
                result = rclone_job.run_quick(
                    remote_path=payload.remote,
                    local_path=payload.local,
                    direction=payload.direction,
                    mode=payload.mode,
                    dry_run=payload.dry_run,
                    extra_args=payload.extra_args,
                    allow_delete=payload.allow_delete,
                    max_delete=payload.max_delete,
                    min_local_files=payload.min_local_files,
                )
                db.job_finish(job_id, _finish_status(result), result)
        except Exception as exc:
            logger.exception("QuickSync #%s fehlgeschlagen", job_id)
            db.job_finish(job_id, "error", {"error": str(exc)})
        finally:
            _remove_job_logger(handler)
            _locks["backup"].release()

    _start_thread(run_quick, job_id=job_id, name=f"quicksync-job-{job_id}")
    return {"ok": True, "job_id": job_id}


@router.get("/list")
def list_jobs(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    q: str = Query("", max_length=200),
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    allowed_kinds = {None, "backup", "check", "quicksync"}
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in allowed_kinds:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")
    return get_db().job_list(
        kind=kind,
        status=status,
        query=q,
        limit=max(1, min(limit, 500)),
        offset=max(0, min(offset, 1_000_000)),
    )


@router.get("/search")
def search_jobs(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    q: str = Query("", max_length=200),
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    allowed_kinds = {None, "backup", "check", "quicksync"}
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in allowed_kinds:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")
    db = get_db()
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, min(offset, 1_000_000))
    return {
        "items": db.job_list(
            kind=kind,
            status=status,
            query=q,
            limit=bounded_limit,
            offset=bounded_offset,
        ),
        "total": db.job_count(kind=kind, status=status, query=q),
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.get("/export.csv")
def export_jobs_csv(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    q: str = Query("", max_length=200),
    limit: int = Query(5000, ge=1, le=10000),
) -> Response:
    allowed_kinds = {None, "backup", "check", "quicksync"}
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in allowed_kinds:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")
    jobs = get_db().job_list(kind=kind, status=status, query=q, limit=limit, offset=0)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, dialect="excel")
    writer.writerow(
        [
            "id",
            "typ",
            "status",
            "gestartet",
            "beendet",
            "dauer_sekunden",
            "zusammenfassung",
        ]
    )
    for job in jobs:
        started = float(job.get("started_at") or 0)
        ended = float(job.get("ended_at") or 0)
        writer.writerow(
            [
                job.get("id"),
                job.get("kind"),
                job.get("status"),
                datetime.fromtimestamp(started).astimezone().isoformat()
                if started
                else "",
                datetime.fromtimestamp(ended).astimezone().isoformat() if ended else "",
                round(max(0.0, ended - started), 3) if started and ended else "",
                json.dumps(
                    job.get("summary") or {}, ensure_ascii=False, separators=(",", ":")
                ),
            ]
        )
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Response(
        "\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=rclone-sync-jobs-{stamp}.csv",
            "Cache-Control": "no-store",
        },
    )


@router.post("/cleanup-failed")
def cleanup_failed_jobs() -> dict[str, Any]:
    return {"ok": True, "deleted": get_db().jobs_delete_failed()}


@router.get("/status/current")
def status_current() -> dict[str, Any]:
    db = get_db()
    return {
        "backup": db.job_running("backup"),
        "check": db.job_running("check"),
        "quicksync": db.job_running("quicksync"),
    }


@router.get("/{job_id}")
def job_detail(job_id: int) -> dict[str, Any]:
    job = get_db().job_get(job_id)
    if not job:
        raise HTTPException(404, "Nicht gefunden")
    return job


@router.get("/{job_id}/log")
def job_log(job_id: int, tail: int = 500) -> dict[str, str]:
    tail = max(1, min(int(tail or 500), 5000))
    job = get_db().job_get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    log_file = job.get("log_file")
    if not log_file:
        return {"log": ""}
    path = Path(log_file).resolve()
    logs_dir = Path(
        get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")
    ).resolve()
    if not is_relative_to(path, logs_dir) or not path.is_file():
        return {"log": ""}
    try:
        # Speicher bleibt auch bei sehr großen Logs begrenzt.
        text = rclone_job.read_log_tail(path, max_bytes=4 * 1024 * 1024)
        return {"log": "\n".join(text.splitlines()[-tail:]) + ("\n" if text else "")}
    except OSError as exc:
        return {"log": f"<Fehler: {exc}>"}


@router.get("/{job_id}/log/download")
def job_log_download(job_id: int) -> Response:
    job = get_db().job_get(job_id)
    if not job:
        raise HTTPException(404, "Job nicht gefunden")
    log_file = job.get("log_file")
    if not log_file:
        raise HTTPException(404, "Für diesen Job ist kein Log vorhanden")
    path = Path(str(log_file)).resolve()
    logs_dir = Path(
        get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")
    ).resolve()
    if not is_relative_to(path, logs_dir) or not path.is_file():
        raise HTTPException(404, "Logdatei nicht gefunden")
    safe_kind = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(job.get("kind") or "job"))
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=f"{safe_kind}-job-{job_id}.log",
        headers={"Cache-Control": "no-store"},
    )
