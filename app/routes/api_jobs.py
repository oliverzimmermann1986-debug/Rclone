"""API für Sync-Jobs: Start, Cancel, Progress, History und Logs."""

from __future__ import annotations

import asyncio
import copy
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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import JobAlreadyRunningError, get_db
from ..job_definitions import definition_pairs, effective_job_definitions
from ..jobs import rclone_sync as rclone_job
from ..jobs import restore_test
from ..jobs import runtime_state
from ..jobs.job_lifecycle import BACKUP_KINDS, reconcile_locked_scope
from ..jobs.locks import HeldFileLock, try_file_lock
from ..jobs.scheduler import rclone_history_key
from ..rclone_args import redact_command_text, rclone_subprocess_env
from ..scheduler_control import pause_scheduler, resume_scheduler, scheduler_state
from ..security import ensure_within, is_relative_to, parse_browse_roots, require_csrf

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_ALLOWED_JOB_KINDS = {None, "backup", "check", "quicksync", "restoretest", "pbs"}

_locks: dict[str, threading.Lock] = {"backup": threading.Lock()}
_SENSITIVE_RESULT_KEYS = {
    "password",
    "password_hash",
    "secret",
    "secret_key",
    "token",
    "credential",
    "credentials",
    "access_key",
    "private_key",
}


def _redact_result(value: Any) -> Any:
    if isinstance(value, str):
        return redact_command_text(value)
    if isinstance(value, list):
        return [_redact_result(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): (
                "***REDACTED***"
                if str(key).casefold() in _SENSITIVE_RESULT_KEYS
                else _redact_result(item)
            )
            for key, item in value.items()
        }
    return value


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


def _finish_runtime_for_job(
    job_id: int, status: str, *, error: str | None = None
) -> None:
    state = runtime_state.load_run_state() or {}
    try:
        state_job_id = int(state.get("job_id") or -1)
    except (TypeError, ValueError):
        return
    if state.get("status") != "running" or state_job_id != job_id:
        return
    run_id = str(state.get("run_id") or "")
    if run_id:
        runtime_state.finish_run(run_id, status, **({"error": error} if error else {}))


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
    job_id: int,
    dry_run: bool,
    pairs_filter: Optional[list[str]],
    history_keys: dict[str, str],
    scope_lock: HeldFileLock,
    run_metadata: dict[str, Any],
    release_locks: bool = True,
) -> None:
    db = None
    handler: logging.FileHandler | None = None
    worker_error: str | None = None
    try:
        db = get_db()
        current = db.job_get(job_id) or {}
        if current.get("status") != "running":
            return
        log_file, handler = _setup_job_logger(job_id, "backup")
        db.job_set_log_file(job_id, str(log_file))
        logger.info(
            "Backup #%s startet (dry_run=%s, pairs=%s)",
            job_id,
            dry_run,
            pairs_filter,
        )
        summary = rclone_job.run_job(
            dry_run=dry_run,
            pairs_filter=pairs_filter,
            trigger="web",
            job_id=job_id,
            defer_runtime_finish=True,
            reset_cancel_state=False,
            **run_metadata,
        )
        summary["history_keys"] = history_keys
        status = _finish_status(summary)
        if status == "cancelled":
            summary.setdefault("error", "Abgebrochen")
            summary["cancelled"] = True
        db.job_finish(job_id, status, summary)
        logger.info("Backup #%s %s", job_id, status)
    except Exception as exc:
        worker_error = str(exc)
        logger.exception("Backup #%s Setup fehlgeschlagen", job_id)
    finally:
        final_db = db
        if final_db is None:
            try:
                final_db = get_db()
            except Exception:
                logger.exception(
                    "DB konnte beim Worker-Abschluss nicht geöffnet werden"
                )
        if final_db is not None:
            if worker_error:
                try:
                    final_db.job_finish(
                        job_id,
                        "error",
                        {"error": f"Setup fehlgeschlagen: {worker_error}"},
                    )
                except Exception:
                    logger.exception("Jobstatus konnte nicht gespeichert werden")
            try:
                actual = final_db.job_get(job_id) or {}
                actual_status = str(actual.get("status") or "")
                if actual_status and actual_status != "running":
                    _finish_runtime_for_job(
                        job_id,
                        actual_status,
                        error=worker_error,
                    )
            except Exception:
                logger.exception("Runtime-Abschluss konnte nicht abgeglichen werden")
        _remove_job_logger(handler)
        if release_locks:
            scope_lock.release()
            _locks["backup"].release()


def _reserve_backup_job(
    kind: str,
    log_file: Optional[str] = None,
    *,
    attempts: list[dict[str, Any]] | None = None,
    definition_id: str | None = None,
    definition_name: str | None = None,
    scheduled_slot: str | None = None,
    config_revision: str,
) -> tuple[int, HeldFileLock]:
    """Reserviert DB und Prozess-Lock ohne abbrechbares Zwischenfenster."""

    scope_lock = try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
    if scope_lock is None:
        _locks["backup"].release()
        raise HTTPException(409, "Ein anderer Sync hält den Prozess-Lock")
    try:
        db = get_db()
        reconciliation = reconcile_locked_scope(
            db,
            scope=runtime_state.DEFAULT_CANCEL_SCOPE,
            kinds=BACKUP_KINDS,
        )
        if not reconciliation.get("safe"):
            raise HTTPException(
                409,
                "Ein registrierter Sync-Unterprozess ist noch aktiv",
            )
        rclone_job.reset_cancel()
        job_id = db.job_start(
            kind,
            log_file=log_file,
            attempts=attempts,
            exclusive_scope=True,
            definition_id=definition_id,
            definition_name=definition_name,
            config_revision=config_revision,
            scheduled_slot=scheduled_slot,
        )
        return job_id, scope_lock
    except HTTPException:
        scope_lock.release()
        _locks["backup"].release()
        raise
    except JobAlreadyRunningError as exc:
        scope_lock.release()
        _locks["backup"].release()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        scope_lock.release()
        _locks["backup"].release()
        logger.exception("Job (%s) konnte nicht angelegt werden", kind)
        raise HTTPException(500, f"Job konnte nicht angelegt werden: {exc}") from exc


def _audit_best_effort(event: str, *, actor: str, details: dict) -> None:
    """Audit darf einen bereits angelegten Job nicht verhindern."""
    try:
        get_db().audit_add(event, actor=actor, details=details)
    except Exception:
        logger.exception("Audit-Event %s konnte nicht gespeichert werden", event)


def _abort_reserved_backup_job(
    job_id: int,
    scope_lock: HeldFileLock,
    *,
    error: str,
    error_code: str,
) -> None:
    """Roll back a DB reservation and both locks after setup failed.

    A reservation is visible as ``running`` immediately.  Therefore every
    failure before the worker owns the lease must make the row terminal before
    releasing the process- and web-locks.  Cleanup itself stays best-effort so
    the original setup error is not hidden by a secondary database problem.
    """

    try:
        get_db().job_finish(
            job_id,
            "error",
            {
                "ok": False,
                "error": error,
                "error_code": error_code,
                "stage": "setup",
            },
        )
    except Exception:
        logger.exception("Fehlgeschlagene Jobreservation #%s blieb unbereinigt", job_id)
    finally:
        scope_lock.release()
        _locks["backup"].release()


def _start_thread(
    target,
    *,
    job_id: int,
    name: str,
    scope_lock: HeldFileLock,
    args: tuple[Any, ...] = (),
) -> None:
    try:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
    except Exception as exc:
        _abort_reserved_backup_job(
            job_id,
            scope_lock,
            error=f"Thread konnte nicht gestartet werden: {exc}",
            error_code="thread_start_failed",
        )
        raise HTTPException(500, "Job konnte nicht gestartet werden") from exc


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
        raise HTTPException(422, str(exc)) from exc


@router.post("/scheduler/resume")
def resume_scheduler_endpoint(user: str = Depends(require_auth)) -> dict[str, Any]:
    return resume_scheduler(actor=user, db=get_db())


def _queue_backup(
    *,
    dry_run: bool,
    pairs_filter: Optional[list[str]],
    definition: dict[str, Any] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    config_revision: str | None = None,
) -> dict[str, Any]:
    if config_snapshot is None:
        config_snapshot, captured_revision = get_config().snapshot_with_revision()
        config_revision = captured_revision
    else:
        config_snapshot = copy.deepcopy(config_snapshot)
    config_revision = str(config_revision or "")
    configured_pairs = [
        pair
        for pair in ((config_snapshot.get("backup") or {}).get("pairs") or [])
        if isinstance(pair, dict)
        and pair.get("enabled", True)
        and (not pairs_filter or str(pair.get("name")) in pairs_filter)
    ]
    history_keys = {
        str(pair.get("name")): rclone_history_key(pair)
        for pair in configured_pairs
        if str(pair.get("name") or "")
    }
    attempts = [
        {
            "name": name,
            "history_key": history_key,
            "trigger": "web",
            "dry_run": dry_run,
        }
        for name, history_key in history_keys.items()
    ]
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    definition_id = str((definition or {}).get("id") or "") or None
    definition_name = str((definition or {}).get("name") or "") or None
    job_id, scope_lock = _reserve_backup_job(
        "backup",
        attempts=attempts,
        definition_id=definition_id,
        definition_name=definition_name,
        config_revision=config_revision,
    )
    try:
        run_metadata = {
            "execution_mode": str(
                (definition or {}).get("execution_mode") or "parallel"
            ),
            "max_parallel_override": (
                int((definition or {}).get("max_parallel") or 1) if definition else None
            ),
            "definition_id": definition_id,
            "definition_name": definition_name,
            "config_revision": config_revision,
            "config_snapshot": config_snapshot,
        }
        _audit_best_effort(
            "backup_requested",
            actor="web",
            details={"dry_run": dry_run, "pairs": pairs_filter or []},
        )
    except Exception as exc:
        logger.exception(
            "Backup #%s konnte nach der Reservation nicht vorbereitet werden", job_id
        )
        _abort_reserved_backup_job(
            job_id,
            scope_lock,
            error=f"Jobvorbereitung fehlgeschlagen: {exc}",
            error_code="setup_failed",
        )
        raise HTTPException(500, "Job konnte nicht vorbereitet werden") from exc
    _start_thread(
        _run_backup_thread,
        job_id=job_id,
        name=f"backup-job-{job_id}",
        scope_lock=scope_lock,
        args=(
            job_id,
            dry_run,
            pairs_filter,
            history_keys,
            scope_lock,
            run_metadata,
        ),
    )
    return {
        "ok": True,
        "job_id": job_id,
        "pairs": pairs_filter,
        "definition_id": definition_id,
        "definition_name": definition_name,
        "config_revision": config_revision,
    }


def _enabled_definition_specs(
    snapshot: dict[str, Any], *, dry_run: bool
) -> list[dict[str, Any]]:
    """Resolve every enabled definition before taking the global run lock.

    Rejecting an invalid enabled definition keeps the batch atomic: no earlier
    definition may start before the caller learns that a later one has no
    active data path.
    """

    specs: list[dict[str, Any]] = []
    invalid: list[str] = []
    for definition in effective_job_definitions(snapshot):
        if not definition.get("enabled", True):
            continue
        pairs = [
            pair
            for pair in definition_pairs(snapshot, definition)
            if pair.get("enabled", True)
        ]
        pair_names = [
            str(pair.get("name") or "").strip()
            for pair in pairs
            if str(pair.get("name") or "").strip()
        ]
        if not pair_names:
            invalid.append(str(definition.get("name") or definition.get("id") or "?"))
            continue
        history_keys = {
            str(pair.get("name")): rclone_history_key(pair) for pair in pairs
        }
        specs.append(
            {
                "definition": definition,
                "pair_names": pair_names,
                "history_keys": history_keys,
                "attempts": [
                    {
                        "name": name,
                        "history_key": history_keys[name],
                        "trigger": "web",
                        "dry_run": dry_run,
                    }
                    for name in pair_names
                ],
            }
        )
    if invalid:
        raise HTTPException(
            409,
            "Aktivierte Jobdefinition(en) ohne aktive Datenwege: " + ", ".join(invalid),
        )
    if not specs:
        raise HTTPException(409, "Keine aktivierte Jobdefinition vorhanden")
    return specs


def _acquire_definition_batch_scope(*, reset_cancel_state: bool = True) -> HeldFileLock:
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    try:
        scope_lock = try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
    except Exception:
        _locks["backup"].release()
        raise
    if scope_lock is None:
        _locks["backup"].release()
        raise HTTPException(409, "Ein anderer Sync hält den Prozess-Lock")
    try:
        reconciliation = reconcile_locked_scope(
            get_db(),
            scope=runtime_state.DEFAULT_CANCEL_SCOPE,
            kinds=BACKUP_KINDS,
        )
        if not reconciliation.get("safe"):
            raise HTTPException(
                409, "Ein registrierter Sync-Unterprozess ist noch aktiv"
            )
        if reset_cancel_state:
            rclone_job.reset_cancel()
        return scope_lock
    except Exception:
        scope_lock.release()
        _locks["backup"].release()
        raise


def _run_definition_batch_thread(batch_id: str, scope_lock: HeldFileLock) -> None:
    """Fuehrt ausschliesslich die zuvor dauerhaft gespeicherten Batch-Items aus."""

    try:
        db = get_db()
        batch = db.job_batch_get(batch_id)
        if not batch:
            logger.error("Persistierter Jobdefinitions-Batch %s fehlt", batch_id)
            return
        dry_run = bool(batch["dry_run"])
        snapshot = batch["snapshot"]
        config_revision = str(batch.get("config_revision") or "")
        first_job_id = next(
            (
                int(item["job_id"])
                for item in batch["items"]
                if item.get("job_id") is not None
            ),
            0,
        )
        for item in batch["items"]:
            if item.get("state") in {"done", "failed", "cancelled"}:
                continue
            index = int(item["position"])
            spec = item["spec"]
            definition = spec["definition"]
            definition_id = str(definition.get("id") or "") or None
            definition_name = str(definition.get("name") or "") or None
            current_batch = db.job_batch_get(batch_id) or {}
            if current_batch.get("cancel_requested") or rclone_job.is_cancelled():
                db.job_batch_item_finish(batch_id, index, "cancelled")
                _audit_best_effort(
                    "job_definition_batch_item_failed",
                    actor="web",
                    details={
                        "batch_job_id": first_job_id,
                        "definition_id": definition_id,
                        "definition_name": definition_name,
                        "state": "failed",
                        "error_code": "batch_cancelled",
                        "error": "Batch wurde vor dem Start dieser Definition abgebrochen",
                    },
                )
                continue
            if item.get("state") == "running" and item.get("job_id") is not None:
                job_id = int(item["job_id"])
            else:
                try:
                    job_id = get_db().job_start(
                        "backup",
                        attempts=spec["attempts"],
                        exclusive_scope=True,
                        definition_id=definition_id,
                        definition_name=definition_name,
                        config_revision=config_revision,
                    )
                    if not db.job_batch_item_start(batch_id, index, job_id):
                        db.job_finish(
                            job_id,
                            "error",
                            {
                                "ok": False,
                                "error": "Batch-Item wurde bereits beansprucht",
                                "error_code": "batch_item_claim_lost",
                            },
                        )
                        continue
                except Exception as exc:
                    logger.exception(
                        "Jobdefinition %s konnte nicht reserviert werden",
                        definition_name,
                    )
                    _audit_best_effort(
                        "job_definition_batch_item_failed",
                        actor="web",
                        details={
                            "batch_job_id": first_job_id,
                            "definition_id": definition_id,
                            "definition_name": definition_name,
                            "state": "failed",
                            "error_code": "reservation_failed",
                            "error": str(exc),
                        },
                    )
                    db.job_batch_item_finish(
                        batch_id, index, "failed", error=str(exc)
                    )
                    continue
            _run_backup_thread(
                job_id,
                dry_run,
                spec["pair_names"],
                spec["history_keys"],
                scope_lock,
                {
                    "execution_mode": str(
                        definition.get("execution_mode") or "sequential"
                    ),
                    "max_parallel_override": int(definition.get("max_parallel") or 1),
                    "definition_id": definition_id,
                    "definition_name": definition_name,
                    "config_revision": config_revision,
                    "config_snapshot": snapshot,
                },
                release_locks=False,
            )
            completed = db.job_get(job_id) or {}
            status = str(completed.get("status") or "stale")
            item_state = (
                "done"
                if status == "ok"
                else "cancelled"
                if status == "cancelled"
                else "failed"
            )
            db.job_batch_item_finish(
                batch_id,
                index,
                item_state,
                error=None if item_state == "done" else f"Jobstatus: {status}",
            )
    finally:
        scope_lock.release()
        _locks["backup"].release()


def _queue_enabled_job_definitions(*, dry_run: bool) -> dict[str, Any]:
    snapshot, revision = get_config().snapshot_with_revision()
    snapshot = copy.deepcopy(snapshot)
    revision = str(revision or "")
    specs = _enabled_definition_specs(snapshot, dry_run=dry_run)
    scope_lock = _acquire_definition_batch_scope()
    definitions = [spec["definition"] for spec in specs]
    first = specs[0]
    first_definition = first["definition"]
    try:
        first_job_id = get_db().job_start(
            "backup",
            attempts=first["attempts"],
            exclusive_scope=True,
            definition_id=str(first_definition.get("id") or "") or None,
            definition_name=str(first_definition.get("name") or "") or None,
            config_revision=revision,
        )
    except JobAlreadyRunningError as exc:
        scope_lock.release()
        _locks["backup"].release()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        scope_lock.release()
        _locks["backup"].release()
        logger.exception("Erste Jobdefinition konnte nicht reserviert werden")
        raise HTTPException(500, "Jobs konnten nicht reserviert werden") from exc
    try:
        batch_id = get_db().job_batch_create(
            specs=specs,
            snapshot=snapshot,
            config_revision=revision,
            dry_run=dry_run,
            first_job_id=first_job_id,
        )
    except Exception as exc:
        _abort_reserved_backup_job(
            first_job_id,
            scope_lock,
            error=f"Batch konnte nicht persistiert werden: {exc}",
            error_code="batch_persist_failed",
        )
        logger.exception("Jobdefinitions-Batch konnte nicht persistiert werden")
        raise HTTPException(500, "Jobs konnten nicht gespeichert werden") from exc
    _audit_best_effort(
        "job_definition_batch_requested",
        actor="web",
        details={
            "dry_run": dry_run,
            "definition_ids": [str(item.get("id") or "") for item in definitions],
        },
    )
    try:
        thread = threading.Thread(
            target=_run_definition_batch_thread,
            args=(batch_id, scope_lock),
            name=f"job-definition-batch-{batch_id[:8]}",
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        _abort_reserved_backup_job(
            first_job_id,
            scope_lock,
            error=f"Batch-Thread konnte nicht gestartet werden: {exc}",
            error_code="thread_start_failed",
        )
        logger.exception("Jobdefinitions-Batch konnte nicht gestartet werden")
        raise HTTPException(500, "Jobs konnten nicht gestartet werden") from exc
    started_definition = {
        "definition_id": str(first_definition.get("id") or ""),
        "definition_name": str(first_definition.get("name") or ""),
        "state": "started",
        "job_id": first_job_id,
    }
    queued_definitions = [
        {
            "definition_id": str(spec["definition"].get("id") or ""),
            "definition_name": str(spec["definition"].get("name") or ""),
            "state": "queued",
            "job_id": None,
        }
        for spec in specs[1:]
    ]
    return {
        "ok": True,
        "job_id": first_job_id,
        "batch_id": batch_id,
        # Backwards-compatible fields now describe only definitions that really
        # own a DB run at response time.  Accepted serial successors are exposed
        # separately as queued instead of being falsely reported as started.
        "definition_ids": [started_definition["definition_id"]],
        "definition_names": [started_definition["definition_name"]],
        "started_definitions": [started_definition],
        "queued_definitions": queued_definitions,
        "failed_definitions": [],
        "definitions": [started_definition, *queued_definitions],
        "config_revision": revision,
        "dry_run": dry_run,
    }


def resume_pending_definition_batches() -> int:
    """Startet nach Webdienst-Restart die noch ausstehende dauerhafte Queue."""
    db = get_db()
    resumed = 0
    for pending in db.job_batches_pending():
        batch_id = str(pending["id"])
        batch = db.job_batch_recover(batch_id) or {}
        if batch.get("state") != "running":
            continue
        try:
            scope_lock = _acquire_definition_batch_scope(
                reset_cancel_state=not bool(batch.get("cancel_requested"))
            )
        except HTTPException as exc:
            logger.info("Batch %s wartet weiter: %s", batch_id, exc.detail)
            continue
        try:
            thread = threading.Thread(
                target=_run_definition_batch_thread,
                args=(batch_id, scope_lock),
                name=f"job-definition-recovery-{batch_id[:8]}",
                daemon=True,
            )
            thread.start()
            resumed += 1
        except Exception:
            scope_lock.release()
            _locks["backup"].release()
            logger.exception("Batch %s konnte nicht wiederaufgenommen werden", batch_id)
        # Es kann konstruktionsbedingt nur einen aktiven Backup-Scope geben.
        break
    return resumed


@router.post("/backup/run")
def run_backup(
    dry_run: bool = Query(False), pairs: Optional[str] = Query(None)
) -> dict[str, Any]:
    if pairs is not None:
        return _queue_backup(dry_run=dry_run, pairs_filter=_parse_pair_filter(pairs))
    return _queue_enabled_job_definitions(dry_run=dry_run)


@router.get("/definitions")
def list_job_definitions() -> list[dict[str, Any]]:
    return effective_job_definitions(get_config())


@router.post("/definitions/run-all")
def run_all_job_definitions(dry_run: bool = Query(False)) -> dict[str, Any]:
    return _queue_enabled_job_definitions(dry_run=dry_run)


def _resolved_definition(
    snapshot: dict[str, Any], definition_id: str
) -> tuple[dict[str, Any], list[str]]:
    definition = next(
        (
            item
            for item in effective_job_definitions(snapshot)
            if str(item.get("id") or "") == definition_id
        ),
        None,
    )
    if definition is None:
        raise HTTPException(404, "Jobdefinition nicht gefunden")
    if not definition.get("enabled", True):
        raise HTTPException(409, "Jobdefinition ist deaktiviert")
    pairs = definition_pairs(snapshot, definition)
    pair_names = [
        str(pair.get("name") or "")
        for pair in pairs
        if pair.get("enabled", True) and str(pair.get("name") or "")
    ]
    if not pair_names:
        raise HTTPException(409, "Jobdefinition hat keine aktiven Datenwege")
    return definition, pair_names


@router.post("/definitions/{definition_id}/run")
def run_job_definition(
    definition_id: str, dry_run: bool = Query(False)
) -> dict[str, Any]:
    snapshot, revision = get_config().snapshot_with_revision()
    definition, pair_names = _resolved_definition(snapshot, definition_id)
    return _queue_backup(
        dry_run=dry_run,
        pairs_filter=pair_names,
        definition=definition,
        config_snapshot=snapshot,
        config_revision=revision,
    )


@router.get("/definitions/{definition_id}/plan")
def plan_job_definition(
    definition_id: str, dry_run: bool = Query(True)
) -> dict[str, Any]:
    snapshot, revision = get_config().snapshot_with_revision()
    definition, pair_names = _resolved_definition(snapshot, definition_id)
    plan = rclone_job.build_job_plan(
        dry_run=dry_run,
        pairs_filter=pair_names,
        config_snapshot=snapshot,
    )
    return {
        **plan,
        "definition_id": definition.get("id"),
        "definition_name": definition.get("name"),
        "config_revision": revision,
        "execution_mode": definition.get("execution_mode"),
        "max_parallel": definition.get("max_parallel"),
    }


@router.get("/backup/plan")
def backup_plan(
    dry_run: bool = Query(True), pairs: Optional[str] = Query(None)
) -> dict[str, Any]:
    return rclone_job.build_job_plan(
        dry_run=dry_run, pairs_filter=_parse_pair_filter(pairs)
    )


@router.post("/backup/cancel")
def cancel_backup(response: Response) -> dict[str, Any]:
    db = get_db()
    queued_batches = db.job_batch_request_cancel()
    running = any(db.job_running(kind) for kind in BACKUP_KINDS)
    state = rclone_job.get_runtime_state() or {}
    if (
        not running
        and not queued_batches
        and state.get("status") != "running"
        and not runtime_state.active_processes()
    ):
        return {"ok": False, "error": "Kein laufender Job"}
    result = rclone_job.cancel_job()
    _audit_best_effort(
        "backup_cancel_requested",
        actor="web",
        details={
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


@router.post("/backup/run-pair/{pair_name}")
def run_single_pair(pair_name: str, dry_run: bool = Query(False)) -> dict[str, Any]:
    if pair_name not in _known_pair_names():
        raise HTTPException(404, "Pair nicht gefunden")
    return _queue_backup(dry_run=dry_run, pairs_filter=[pair_name])


@router.post("/backup/check/{pair_name}")
def check_pair(
    pair_name: str,
    one_way: Optional[bool] = Query(None),
    download: bool = Query(False),
) -> dict[str, Any]:
    if pair_name not in _known_pair_names():
        raise HTTPException(404, "Pair nicht gefunden")
    # Snapshot before taking either lock.  A config read failure must never
    # leave the process looking busy although no DB reservation exists.
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup/Check läuft bereits")
    job_id, scope_lock = _reserve_backup_job("check", config_revision=config_revision)
    _audit_best_effort(
        "check_requested",
        actor="web",
        details={"pair": pair_name, "one_way": one_way, "download": download},
    )

    def run_check() -> None:
        db = None
        handler: logging.FileHandler | None = None
        worker_error: str | None = None
        try:
            db = get_db()
            current = db.job_get(job_id) or {}
            if current.get("status") != "running":
                return
            log_file, handler = _setup_job_logger(job_id, "check")
            db.job_set_log_file(job_id, str(log_file))
            result = rclone_job.run_pair_check(
                pair_name,
                one_way=one_way,
                download=download,
                reset_cancel_state=False,
                config_snapshot=config_snapshot,
            )
            db.job_finish(job_id, _finish_status(result), result)
        except Exception as exc:
            worker_error = str(exc)
            logger.exception("Check #%s fehlgeschlagen", job_id)
        finally:
            final_db = db
            if final_db is None:
                try:
                    final_db = get_db()
                except Exception:
                    logger.exception(
                        "DB konnte beim Check-Abschluss nicht geöffnet werden"
                    )
            if final_db is not None and worker_error:
                try:
                    final_db.job_finish(
                        job_id,
                        "error",
                        {"error": worker_error},
                    )
                except Exception:
                    logger.exception(
                        "Check-Fehlerstatus konnte nicht gespeichert werden"
                    )
            _remove_job_logger(handler)
            scope_lock.release()
            _locks["backup"].release()

    _start_thread(
        run_check,
        job_id=job_id,
        name=f"check-job-{job_id}",
        scope_lock=scope_lock,
    )
    return {"ok": True, "job_id": job_id}


@router.post("/backup/restore-test")
def start_restore_test(pairs: Optional[str] = Query(None)) -> dict[str, Any]:
    """Holt Stichproben aus dem Ziel zurück und vergleicht sie mit der Quelle.

    Teilt sich den Backup-Scope, damit kein Drill gegen einen halb
    geschriebenen Zwischenstand prüft.
    """
    pairs_filter = _parse_pair_filter(pairs)
    known = _known_pair_names()
    unknown = [name for name in (pairs_filter or []) if name not in known]
    if unknown:
        raise HTTPException(404, f"Pair nicht gefunden: {', '.join(unknown)}")
    # See ``check_pair``: snapshot failures happen outside the lock lifecycle.
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup, Check oder Drill läuft bereits")
    job_id, scope_lock = _reserve_backup_job(
        restore_test.JOB_KIND, config_revision=config_revision
    )
    _audit_best_effort(
        "restore_test_requested",
        actor="web",
        details={"pairs": pairs_filter or []},
    )

    def run_drill() -> None:
        db = None
        handler: logging.FileHandler | None = None
        worker_error: str | None = None
        try:
            db = get_db()
            current = db.job_get(job_id) or {}
            if current.get("status") != "running":
                return
            log_file, handler = _setup_job_logger(job_id, restore_test.JOB_KIND)
            db.job_set_log_file(job_id, str(log_file))
            result = restore_test.run_restore_test(
                pairs_filter=pairs_filter,
                trigger="manual",
                reset_cancel_state=False,
                config_snapshot=config_snapshot,
            )
            db.job_finish(job_id, _finish_status(result), result)
        except Exception as exc:
            worker_error = str(exc)
            logger.exception("Restore-Drill #%s fehlgeschlagen", job_id)
        finally:
            final_db = db
            if final_db is None:
                try:
                    final_db = get_db()
                except Exception:
                    logger.exception(
                        "DB konnte beim Drill-Abschluss nicht geöffnet werden"
                    )
            if final_db is not None and worker_error:
                try:
                    final_db.job_finish(job_id, "error", {"error": worker_error})
                except Exception:
                    logger.exception(
                        "Drill-Fehlerstatus konnte nicht gespeichert werden"
                    )
            _remove_job_logger(handler)
            scope_lock.release()
            _locks["backup"].release()

    _start_thread(
        run_drill,
        job_id=job_id,
        name=f"restore-test-job-{job_id}",
        scope_lock=scope_lock,
    )
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
        return {
            "running": False,
            "last": _redact_result(last[0]) if last else None,
        }

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
            "last_progress_at": pair_state.get("last_progress_at"),
            "stall_timeout_sec": pair_state.get("stall_timeout_sec"),
            "max_runtime_sec": pair_state.get("max_runtime_sec"),
            **_latest_stats(text),
        }
        pairs_status.append(item)
    pairs_status.sort(key=lambda item: str(item["name"]).casefold())
    finished = {"done", "error", "cancelled", "skipped"}
    return _redact_result(
        {
            "running": True,
            "job_id": running["id"],
            "started_at": started,
            "elapsed_sec": max(0, round(time.time() - started)),
            "pairs": pairs_status,
            "total_pairs": len(pairs_status),
            "done_pairs": sum(1 for pair in pairs_status if pair["status"] in finished),
        }
    )


@router.get("/progress/stream")
async def progress_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events für Live-Progress.

    Der bestehende Polling-Endpoint ``/backup/progress`` bleibt als Fallback
    erhalten. Der synchrone Snapshot wird im Threadpool erzeugt, damit der
    einzelne Uvicorn-Worker während des Log-Lesens nicht blockiert.
    """

    async def event_generator():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await asyncio.to_thread(backup_progress)
                payload = json.dumps(data, default=str)
                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
            except Exception:
                yield f"data: {json.dumps({'error': 'progress unavailable'})}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


def _validate_quick_paths(
    payload: QuickSyncPayload, config_snapshot: dict[str, Any] | None = None
) -> tuple[str, str]:
    if config_snapshot is None:
        cfg = get_config()
        config_snapshot = {
            "web": {
                "local_browse_roots": cfg.get(
                    "web",
                    "local_browse_roots",
                    default=["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
                )
            },
            "backup": cfg.get("backup", default={}) or {},
        }
    if any(char in payload.remote + payload.local for char in ("\x00", "\n", "\r")):
        raise HTTPException(400, "Pfad enthält ungültige Zeichen")
    if not Path(payload.local).is_absolute():
        raise HTTPException(400, "Lokaler Pfad muss absolut sein")
    roots = parse_browse_roots(
        ((config_snapshot.get("web") or {}).get("local_browse_roots"))
        or ["/mnt", "/media", "/srv", "/opt/rclone-sync/data"]
    )
    if not roots:
        raise HTTPException(503, "Keine lokalen Quick-Sync-Wurzeln konfiguriert")
    local_path = ensure_within(Path(payload.local), roots)
    remote_is_local = (
        payload.remote.startswith("/") or Path(payload.remote).is_absolute()
    )
    if not remote_is_local and (
        ":" not in payload.remote or payload.remote.startswith((":", "-"))
    ):
        raise HTTPException(
            400,
            "Remote muss ein konfigurierter rclone-Pfad oder ein absoluter "
            "lokaler Pfad sein",
        )
    if remote_is_local:
        remote_path = ensure_within(Path(payload.remote), roots)
        if (
            remote_path == local_path
            or is_relative_to(remote_path, local_path)
            or is_relative_to(local_path, remote_path)
        ):
            raise HTTPException(400, "Lokale Quelle und Ziel dürfen nicht überlappen")
        _finish_quick_validation(payload, config_snapshot)
        return str(remote_path), str(local_path)
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
        raise HTTPException(
            503, f"rclone-Remotes konnten nicht geprüft werden: {exc}"
        ) from exc
    remotes = (
        {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if result.returncode == 0
        else set()
    )
    remote_name = payload.remote.split(":", 1)[0] + ":"
    if remote_name not in remotes:
        raise HTTPException(403, "Remote ist nicht konfiguriert")

    _finish_quick_validation(payload, config_snapshot)
    return payload.remote, str(local_path)


def _finish_quick_validation(
    payload: QuickSyncPayload, config_snapshot: dict[str, Any] | None = None
) -> None:
    if config_snapshot is None:
        config_snapshot = {"backup": get_config().get("backup", default={}) or {}}
    if payload.direction == "bisync" and payload.mode != "bisync":
        raise HTTPException(400, "Bei direction=bisync muss mode=bisync sein")
    if payload.direction != "bisync" and payload.mode == "bisync":
        raise HTTPException(400, "Bei pull/push muss mode copy oder sync sein")
    if payload.mode in {"sync", "bisync"} and not payload.dry_run:
        backup = config_snapshot.get("backup") or {}
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
    config_snapshot, config_revision = get_config().snapshot_with_revision()
    remote_path, local_path = _validate_quick_paths(payload, config_snapshot)
    if not _locks["backup"].acquire(blocking=False):
        raise HTTPException(409, "Backup läuft bereits")
    job_id, scope_lock = _reserve_backup_job(
        "quicksync", config_revision=config_revision
    )
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
        db = None
        handler: logging.FileHandler | None = None
        worker_error: str | None = None
        try:
            db = get_db()
            current = db.job_get(job_id) or {}
            if current.get("status") != "running":
                return
            log_file, handler = _setup_job_logger(job_id, "quicksync")
            db.job_set_log_file(job_id, str(log_file))
            result = rclone_job.run_quick(
                remote_path=remote_path,
                local_path=local_path,
                direction=payload.direction,
                mode=payload.mode,
                dry_run=payload.dry_run,
                extra_args=payload.extra_args,
                allow_delete=payload.allow_delete,
                max_delete=payload.max_delete,
                min_local_files=payload.min_local_files,
                reset_cancel_state=False,
                config_snapshot=config_snapshot,
            )
            db.job_finish(job_id, _finish_status(result), result)
        except Exception as exc:
            worker_error = str(exc)
            logger.exception("QuickSync #%s fehlgeschlagen", job_id)
        finally:
            final_db = db
            if final_db is None:
                try:
                    final_db = get_db()
                except Exception:
                    logger.exception(
                        "DB konnte beim QuickSync-Abschluss nicht geöffnet werden"
                    )
            if final_db is not None and worker_error:
                try:
                    final_db.job_finish(
                        job_id,
                        "error",
                        {"error": worker_error},
                    )
                except Exception:
                    logger.exception(
                        "QuickSync-Fehlerstatus konnte nicht gespeichert werden"
                    )
            _remove_job_logger(handler)
            scope_lock.release()
            _locks["backup"].release()

    _start_thread(
        run_quick,
        job_id=job_id,
        name=f"quicksync-job-{job_id}",
        scope_lock=scope_lock,
    )
    return {"ok": True, "job_id": job_id}


@router.get("/list")
def list_jobs(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    q: str = Query("", max_length=200),
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in _ALLOWED_JOB_KINDS:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")
    return [
        _redact_result(job)
        for job in get_db().job_list(
            kind=kind,
            status=status,
            query=q,
            limit=max(1, min(limit, 500)),
            offset=max(0, min(offset, 1_000_000)),
        )
    ]


@router.get("/search")
def search_jobs(
    kind: Optional[str] = None,
    status: Optional[str] = None,
    q: str = Query("", max_length=200),
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in _ALLOWED_JOB_KINDS:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")
    db = get_db()
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, min(offset, 1_000_000))
    return {
        "items": [
            _redact_result(job)
            for job in db.job_list(
                kind=kind,
                status=status,
                query=q,
                limit=bounded_limit,
                offset=bounded_offset,
            )
        ],
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
) -> StreamingResponse:
    allowed_status = {None, "running", "ok", "error", "skipped", "cancelled", "stale"}
    if kind not in _ALLOWED_JOB_KINDS:
        raise HTTPException(400, "Unbekannter Job-Typ")
    if status not in allowed_status:
        raise HTTPException(400, "Unbekannter Job-Status")

    def rows():
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, dialect="excel")

        def emit(row: list[Any]) -> str:
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(row)
            return buffer.getvalue()

        yield "\ufeff" + emit(
            [
                "id",
                "typ",
                "definition_id",
                "definition_name",
                "config_revision",
                "scheduled_slot",
                "status",
                "gestartet",
                "beendet",
                "dauer_sekunden",
                "zusammenfassung",
            ]
        )
        exported = 0
        page_size = min(250, limit)
        while exported < limit:
            requested = min(page_size, limit - exported)
            jobs = get_db().job_list(
                kind=kind,
                status=status,
                query=q,
                limit=requested,
                offset=exported,
            )
            if not jobs:
                break
            for job in jobs:
                started = float(job.get("started_at") or 0)
                ended = float(job.get("ended_at") or 0)
                yield emit(
                    [
                        job.get("id"),
                        job.get("kind"),
                        job.get("definition_id"),
                        job.get("definition_name"),
                        job.get("config_revision"),
                        job.get("scheduled_slot"),
                        job.get("status"),
                        datetime.fromtimestamp(started).astimezone().isoformat()
                        if started
                        else "",
                        datetime.fromtimestamp(ended).astimezone().isoformat()
                        if ended
                        else "",
                        round(max(0.0, ended - started), 3)
                        if started and ended
                        else "",
                        json.dumps(
                            _redact_result(job.get("summary") or {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ]
                )
            exported += len(jobs)
            if len(jobs) < requested:
                break

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=rclone-sync-jobs-{stamp}.csv",
            "Cache-Control": "no-store",
        },
    )


@router.post("/cleanup-failed")
def cleanup_failed_jobs() -> dict[str, Any]:
    deleted = get_db().jobs_delete_failed()
    _audit_best_effort("failed_jobs_cleaned", actor="web", details={"deleted": deleted})
    return {"ok": True, "deleted": deleted}


@router.post("/{job_id}/retry")
def retry_job(job_id: int, dry_run: bool = Query(False)) -> dict[str, Any]:
    previous = get_db().job_get(job_id)
    if not previous:
        raise HTTPException(404, "Job nicht gefunden")
    if previous.get("status") == "running":
        raise HTTPException(409, "Ein laufender Job kann nicht erneut gestartet werden")
    if previous.get("status") not in {"error", "cancelled", "stale"}:
        raise HTTPException(
            409,
            "Nur fehlgeschlagene, abgebrochene oder veraltete Jobs können erneut gestartet werden",
        )
    if previous.get("kind") != "backup":
        raise HTTPException(
            409,
            "Nur definitionsbasierte Sicherungsjobs können sicher wiederholt werden",
        )
    definition_id = str(previous.get("definition_id") or "").strip()
    previous_revision = str(previous.get("config_revision") or "").strip()
    if not definition_id or not previous_revision:
        raise HTTPException(
            409,
            "Der alte Lauf enthält keine revisionsgebundene Jobdefinition",
        )
    snapshot, current_revision = get_config().snapshot_with_revision()
    current_revision = str(current_revision or "")
    if current_revision != previous_revision:
        raise HTTPException(
            409,
            "Die Konfiguration wurde seit diesem Lauf geändert. Bitte Job neu planen und bewusst starten.",
        )
    definition, pair_names = _resolved_definition(snapshot, definition_id)
    result = _queue_backup(
        dry_run=dry_run,
        pairs_filter=pair_names,
        definition=definition,
        config_snapshot=snapshot,
        config_revision=current_revision,
    )
    _audit_best_effort(
        "backup_retried",
        actor="web",
        details={
            "source_job_id": job_id,
            "new_job_id": result.get("job_id"),
            "definition_id": definition_id,
            "dry_run": dry_run,
        },
    )
    return {**result, "retry_of_job_id": job_id}


@router.get("/status/current")
def status_current() -> dict[str, Any]:
    db = get_db()
    return _redact_result(
        {
            "backup": db.job_running("backup"),
            "check": db.job_running("check"),
            "quicksync": db.job_running("quicksync"),
            "restoretest": db.job_running("restoretest"),
            "pbs": db.job_running("pbs"),
        }
    )


@router.get("/{job_id}")
def job_detail(job_id: int) -> dict[str, Any]:
    job = get_db().job_get(job_id)
    if not job:
        raise HTTPException(404, "Nicht gefunden")
    return _redact_result(job)


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
        text = redact_command_text(
            rclone_job.read_log_tail(path, max_bytes=4 * 1024 * 1024)
        )
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

    def sanitized_log():
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                yield redact_command_text(line)

    return StreamingResponse(
        sanitized_log(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{safe_kind}-job-{job_id}.log"'
            ),
        },
    )
