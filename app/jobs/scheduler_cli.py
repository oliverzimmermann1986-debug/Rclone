"""CLI: minütlich aufgerufen, triggert fällige Pairs."""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from ..config_store import get_config
from ..db import get_db
from ..job_definitions import data_paths_by_id
from ..overdue import check_and_notify as check_overdue
from ..scheduler_control import scheduler_state
from . import runtime_state
from .job_lifecycle import (
    BACKUP_KINDS,
    PBS_KINDS,
    reconcile_locked_scope,
)
from .locks import file_lock_or_none
from .pbs_backup import PBS_CANCEL_SCOPE, run_pbs_backup
from .rclone_sync import reset_cancel, run_job
from .restore_test import AGGREGATE_RUN_NAME as RESTORE_AGGREGATE_NAME
from .restore_test import JOB_KIND as RESTORE_JOB_KIND
from .restore_test import run_restore_test
from .scheduler import (
    RESTORE_TEST_HISTORY_KEY,
    find_due_pairs,
    find_due_pbs_targets,
    rclone_history_key,
    restore_test_due,
)


def _job_status(summary: dict) -> str:
    pairs = summary.get("pairs") or []
    cancelled = summary.get("cancelled") is True or any(
        isinstance(pair, dict) and pair.get("cancelled") is True for pair in pairs
    )
    if cancelled:
        return "cancelled"
    return "ok" if summary.get("ok") else "error"


def _attempt_metadata(
    due: list[str], status: list[dict]
) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    wanted = set(due)
    attempts: list[dict] = []
    history_keys: dict[str, str] = {}
    scheduler_slots: dict[str, str] = {}
    for item in status:
        if item.get("name") not in wanted or not item.get("due"):
            continue
        run_name = str(item.get("run_name") or item.get("name") or "").strip()
        history_key = str(item.get("history_key") or "").strip()
        scheduled_slot = str(item.get("scheduled_slot") or "").strip()
        if not run_name or not history_key:
            continue
        history_keys[run_name] = history_key
        if scheduled_slot:
            scheduler_slots[run_name] = scheduled_slot
        attempts.append(
            {
                "name": run_name,
                "history_key": history_key,
                "scheduled_slot": scheduled_slot,
                "trigger": "scheduler",
            }
        )
    return attempts, history_keys, scheduler_slots


def _with_metadata(
    summary: dict,
    *,
    history_keys: dict[str, str],
    scheduler_slots: dict[str, str],
) -> dict:
    enriched = dict(summary)
    enriched.setdefault("trigger", "scheduler")
    enriched["history_keys"] = history_keys
    enriched["scheduler_slots"] = scheduler_slots
    return enriched


def _definition_attempt_metadata(
    definition: dict, cfg
) -> tuple[list[str], list[dict], dict[str, str], dict[str, str]]:
    indexed = data_paths_by_id(cfg)
    names: list[str] = []
    attempts: list[dict] = []
    history_keys: dict[str, str] = {}
    scheduler_slots: dict[str, str] = {}
    slot = str(definition.get("scheduled_slot") or "").strip()
    for path_id in definition.get("data_path_ids") or []:
        pair = indexed.get(str(path_id))
        if not pair or not pair.get("enabled", True):
            continue
        name = str(pair.get("name") or "").strip()
        if not name:
            continue
        history_key = rclone_history_key(pair)
        names.append(name)
        history_keys[name] = history_key
        if slot:
            scheduler_slots[name] = slot
        attempts.append(
            {
                "name": name,
                "history_key": history_key,
                "scheduled_slot": slot,
                "trigger": "scheduler",
            }
        )
    return names, attempts, history_keys, scheduler_slots


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


def _configure_logging(log_file: Path | None = None) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        handlers.insert(0, logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _snapshot_with_revision(cfg) -> tuple[dict, str]:
    capture = getattr(cfg, "snapshot_with_revision", None)
    if callable(capture):
        return capture()
    snapshot = getattr(cfg, "snapshot", None)
    if callable(snapshot):
        return snapshot(), str(getattr(cfg, "revision", "") or "")
    data = getattr(cfg, "data", None)
    if isinstance(data, dict):
        return copy.deepcopy(data), str(getattr(cfg, "revision", "") or "")
    return {
        "backup": cfg.get("backup", default={}) or {},
        "paths": cfg.get("paths", default={}) or {},
    }, str(getattr(cfg, "revision", "") or "")


def _reconcile_available_scopes(db) -> None:
    logger = logging.getLogger("scheduler_cli")
    for scope, kinds in (
        (runtime_state.DEFAULT_CANCEL_SCOPE, BACKUP_KINDS),
        (PBS_CANCEL_SCOPE, PBS_KINDS),
    ):
        with file_lock_or_none(scope) as got_lock:
            if got_lock is None:
                continue
            result = reconcile_locked_scope(db, scope=scope, kinds=kinds)
            if result.get("recovered_jobs"):
                logger.warning(
                    "%d verwaiste %s-Job(s) als stale markiert",
                    result["recovered_jobs"],
                    scope,
                )
            if not result.get("safe"):
                logger.error(
                    "Scope %s ist lock-frei, hat aber aktive Unterprozesse",
                    scope,
                )


def main() -> int:
    cfg = get_config()
    scheduler_snapshot, scheduler_config_revision = _snapshot_with_revision(cfg)
    db = get_db()
    backup = scheduler_snapshot.get("backup") or {}
    backup_enabled = bool(backup.get("enabled", True))

    _reconcile_available_scopes(db)

    # Vor der Pausenprüfung: ein Wartungsfenster darf ein seit Tagen totes Pair
    # nicht stummschalten. Nur bei bewusst abgeschalteten Zeitplänen entfällt
    # die Alarmierung, sonst meldete sie dauerhaft.
    if backup_enabled:
        try:
            reported = check_overdue(scheduler_snapshot, db)
        except Exception:
            logging.getLogger("scheduler_cli").exception(
                "Ausbleib-Prüfung fehlgeschlagen"
            )
        else:
            if reported:
                _configure_logging(None)
                logging.getLogger("scheduler_cli").warning(
                    "Ausbleib-Alarm für %s", ", ".join(reported)
                )

    control = scheduler_state(db)
    if control.get("paused"):
        _configure_logging(None)
        logging.getLogger("scheduler_cli").info(
            "Automatischer Scheduler pausiert bis %s (%s)",
            control.get("until"),
            control.get("reason") or "ohne Grund",
        )
        return 0

    if backup_enabled:
        due, status = find_due_pairs(scheduler_snapshot, db)
    else:
        due, status = (
            [],
            [
                {
                    "name": str(pair.get("name") or "?"),
                    "due": False,
                    "reason": "backup_disabled",
                }
                for pair in (backup.get("pairs") or [])
                if isinstance(pair, dict)
            ],
        )
    pbs_due, _pbs_status = find_due_pbs_targets(cfg, db)
    restore_due = restore_test_due(cfg, db) if backup_enabled else {"due": False}

    if not due and not pbs_due and not restore_due.get("due"):
        # Normalfall: kein Logfile pro Minute erzeugen.
        _configure_logging(None)
        logging.getLogger("scheduler_cli").info(
            "Keine fälligen Pairs (%d rclone, %d PBS geprüft)",
            len(status),
            len(_pbs_status),
        )
        return 0

    rc = 0

    log_dir = Path(
        (scheduler_snapshot.get("paths") or {}).get("logs_dir", "/opt/rclone-sync/logs")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"scheduler-{datetime.now():%Y%m%d-%H%M%S}.log"
    try:
        log_file.touch(mode=0o600, exist_ok=True)
        os.chmod(log_file, 0o600)
    except OSError:
        pass
    _configure_logging(log_file)
    logger = logging.getLogger("scheduler_cli")

    if due:
        logger.info("Fällige Pairs: %s", due)
        with file_lock_or_none("backup") as got_lock:
            if got_lock is None:
                logger.warning("Sync läuft bereits - überspringe diesen Tick")
            else:
                reconciliation = reconcile_locked_scope(
                    db,
                    scope=runtime_state.DEFAULT_CANCEL_SCOPE,
                    kinds=BACKUP_KINDS,
                )
                if not reconciliation.get("safe"):
                    logger.error(
                        "Registrierter Sync-Unterprozess ist noch aktiv; Tick abgebrochen"
                    )
                    return 1
                # Die erste Fälligkeitsprüfung liegt vor dem non-blocking Lock.
                # Nach einem gerade abgeschlossenen konkurrierenden Lauf kann
                # ihr Ergebnis bereits veraltet sein, daher unter dem Lock mit
                # frischer Historie erneut prüfen.
                scheduler_snapshot, config_revision = _snapshot_with_revision(cfg)
                due, status = find_due_pairs(scheduler_snapshot, db)
                due_definitions = [
                    item
                    for item in status
                    if item.get("due") and item.get("name") in set(due)
                ]
                if not due_definitions:
                    logger.info("Rclone-Fälligkeit nach Lock-Erwerb bereits erledigt")
                else:
                    for definition in due_definitions:
                        (
                            pair_names,
                            attempts,
                            history_keys,
                            scheduler_slots,
                        ) = _definition_attempt_metadata(definition, scheduler_snapshot)
                        if not pair_names:
                            logger.warning(
                                "Jobdefinition %s hat keine aktiven Datenwege",
                                definition.get("name"),
                            )
                            continue
                        reset_cancel()
                        job_id = db.job_start(
                            "backup",
                            log_file=str(log_file),
                            attempts=attempts,
                            exclusive_scope=True,
                            definition_id=str(definition.get("definition_id") or ""),
                            definition_name=str(definition.get("name") or ""),
                            config_revision=config_revision,
                            scheduled_slot=str(definition.get("scheduled_slot") or ""),
                        )
                        try:
                            summary = run_job(
                                dry_run=False,
                                pairs_filter=pair_names,
                                trigger="scheduler",
                                job_id=job_id,
                                defer_runtime_finish=True,
                                reset_cancel_state=False,
                                execution_mode=str(
                                    definition.get("execution_mode") or "sequential"
                                ),
                                max_parallel_override=int(
                                    definition.get("max_parallel") or 1
                                ),
                                definition_id=str(
                                    definition.get("definition_id") or ""
                                ),
                                definition_name=str(definition.get("name") or ""),
                                config_revision=config_revision,
                                scheduled_slot=str(
                                    definition.get("scheduled_slot") or ""
                                ),
                                config_snapshot=scheduler_snapshot,
                            )
                            summary = _with_metadata(
                                summary,
                                history_keys=history_keys,
                                scheduler_slots=scheduler_slots,
                            )
                            status_name = _job_status(summary)
                            transitioned = db.job_finish(job_id, status_name, summary)
                            if transitioned:
                                _finish_runtime_for_job(job_id, status_name)
                            else:
                                actual = db.job_get(job_id) or {}
                                _finish_runtime_for_job(
                                    job_id, str(actual.get("status") or "stale")
                                )
                            logger.info(
                                "Scheduler-Job %s fertig: %s",
                                definition.get("name"),
                                json.dumps(summary, ensure_ascii=False, default=str)[
                                    :1000
                                ],
                            )
                            if status_name != "ok":
                                rc = 1
                        except Exception as e:
                            logger.exception("Scheduler-Run gescheitert: %s", e)
                            transitioned = db.job_finish(
                                job_id,
                                "error",
                                {
                                    "ok": False,
                                    "error": str(e),
                                    "due": pair_names,
                                    "trigger": "scheduler",
                                    "history_keys": history_keys,
                                    "scheduler_slots": scheduler_slots,
                                    "definition_id": definition.get("definition_id"),
                                    "definition_name": definition.get("name"),
                                    "config_revision": config_revision,
                                    "scheduled_slot": definition.get("scheduled_slot"),
                                },
                            )
                            if transitioned:
                                _finish_runtime_for_job(job_id, "error", error=str(e))
                            else:
                                actual = db.job_get(job_id) or {}
                                _finish_runtime_for_job(
                                    job_id,
                                    str(actual.get("status") or "stale"),
                                    error=str(e),
                                )
                            rc = 1

    if pbs_due:
        logger.info("Fällige PBS-Targets: %s", pbs_due)
        with file_lock_or_none("pbs") as got_lock:
            if got_lock is None:
                logger.warning("PBS-Backup läuft bereits - überspringe diesen Tick")
            else:
                reconciliation = reconcile_locked_scope(
                    db,
                    scope=PBS_CANCEL_SCOPE,
                    kinds=PBS_KINDS,
                )
                if not reconciliation.get("safe"):
                    logger.error(
                        "Registrierter PBS-Unterprozess ist noch aktiv; Tick abgebrochen"
                    )
                    return 1
                scheduler_snapshot, scheduler_config_revision = _snapshot_with_revision(
                    cfg
                )
                pbs_due, _pbs_status = find_due_pbs_targets(scheduler_snapshot, db)
                if not pbs_due:
                    logger.info("PBS-Fälligkeit nach Lock-Erwerb bereits erledigt")
                    return rc
                (
                    pbs_attempts,
                    pbs_history_keys,
                    pbs_scheduler_slots,
                ) = _attempt_metadata(pbs_due, _pbs_status)
                pbs_run_names = [str(item["name"]) for item in pbs_attempts]
                reset_cancel(PBS_CANCEL_SCOPE)
                job_id = db.job_start(
                    "pbs",
                    log_file=str(log_file),
                    attempts=pbs_attempts,
                    exclusive_scope=True,
                    config_revision=scheduler_config_revision,
                )
                try:
                    summary = run_pbs_backup(
                        pbs_due,
                        trigger="scheduler",
                        reset_cancel_state=False,
                        config_snapshot=scheduler_snapshot,
                    )
                    summary = _with_metadata(
                        summary,
                        history_keys=pbs_history_keys,
                        scheduler_slots=pbs_scheduler_slots,
                    )
                    status_name = _job_status(summary)
                    db.job_finish(job_id, status_name, summary)
                    if status_name != "ok":
                        rc = 1
                except Exception as e:
                    logger.exception("PBS-Scheduler-Run gescheitert: %s", e)
                    db.job_finish(
                        job_id,
                        "error",
                        {
                            "ok": False,
                            "error": str(e),
                            "due": pbs_run_names,
                            "trigger": "scheduler",
                            "history_keys": pbs_history_keys,
                            "scheduler_slots": pbs_scheduler_slots,
                        },
                    )
                    rc = 1

    # Der Drill teilt sich den Backup-Scope. Lief in diesem Tick bereits ein
    # Sync, wartet er auf den nächsten — seine Fälligkeit bleibt bestehen, weil
    # kein Lauf verbucht wurde.
    if restore_due.get("due") and not due:
        logger.info("Restore-Drill ist fällig")
        with file_lock_or_none("backup") as got_lock:
            if got_lock is None:
                logger.warning("Backup-Scope belegt - Restore-Drill verschoben")
            else:
                reconciliation = reconcile_locked_scope(
                    db,
                    scope=runtime_state.DEFAULT_CANCEL_SCOPE,
                    kinds=BACKUP_KINDS,
                )
                if not reconciliation.get("safe"):
                    logger.error(
                        "Registrierter Sync-Unterprozess ist noch aktiv; "
                        "Restore-Drill abgebrochen"
                    )
                    return 1
                scheduler_snapshot, scheduler_config_revision = _snapshot_with_revision(
                    cfg
                )
                restore_due = restore_test_due(scheduler_snapshot, db)
                if not restore_due.get("due"):
                    logger.info("Restore-Fälligkeit nach Lock-Erwerb bereits erledigt")
                    return rc
                slot = str(restore_due.get("scheduled_slot") or "")
                history_keys = {RESTORE_AGGREGATE_NAME: RESTORE_TEST_HISTORY_KEY}
                scheduler_slots = {RESTORE_AGGREGATE_NAME: slot} if slot else {}
                reset_cancel()
                job_id = db.job_start(
                    RESTORE_JOB_KIND,
                    log_file=str(log_file),
                    attempts=[
                        {
                            "name": RESTORE_AGGREGATE_NAME,
                            "history_key": RESTORE_TEST_HISTORY_KEY,
                            "scheduled_slot": slot,
                            "trigger": "scheduler",
                        }
                    ],
                    exclusive_scope=True,
                    config_revision=scheduler_config_revision,
                )
                try:
                    summary = run_restore_test(
                        trigger="scheduler",
                        reset_cancel_state=False,
                        config_snapshot=scheduler_snapshot,
                    )
                    summary = _with_metadata(
                        summary,
                        history_keys={
                            **(summary.get("history_keys") or {}),
                            **history_keys,
                        },
                        scheduler_slots=scheduler_slots,
                    )
                    status_name = _job_status(summary)
                    db.job_finish(job_id, status_name, summary)
                    logger.info(
                        "Restore-Drill fertig: %s von %s Stichproben identisch",
                        summary.get("verified_files"),
                        summary.get("sampled_files"),
                    )
                    if status_name != "ok":
                        rc = 1
                except Exception as e:
                    logger.exception("Restore-Drill gescheitert: %s", e)
                    db.job_finish(
                        job_id,
                        "error",
                        {
                            "ok": False,
                            "error": str(e),
                            "trigger": "scheduler",
                            "history_keys": history_keys,
                            "scheduler_slots": scheduler_slots,
                        },
                    )
                    rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
