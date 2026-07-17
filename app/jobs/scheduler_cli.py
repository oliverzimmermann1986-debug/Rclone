"""CLI: minütlich aufgerufen, triggert fällige Pairs."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from ..config_store import get_config
from ..db import get_db
from ..scheduler_control import scheduler_state
from .locks import file_lock_or_none
from .pbs_backup import run_pbs_backup
from .rclone_sync import run_job
from .scheduler import find_due_pairs, find_due_pbs_targets


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


def main() -> int:
    cfg = get_config()
    db = get_db()
    backup = cfg.get("backup", default={}) or {}
    if not bool(backup.get("enabled", True)):
        _configure_logging(None)
        logging.getLogger("scheduler_cli").info(
            "Automatischer Scheduler ist in der Konfiguration deaktiviert"
        )
        return 0

    control = scheduler_state(db)
    if control.get("paused"):
        _configure_logging(None)
        logging.getLogger("scheduler_cli").info(
            "Automatischer Scheduler pausiert bis %s (%s)",
            control.get("until"),
            control.get("reason") or "ohne Grund",
        )
        return 0

    due, status = find_due_pairs(cfg, db)
    pbs_due, _pbs_status = find_due_pbs_targets(cfg, db)

    if not due and not pbs_due:
        # Normalfall: kein Logfile pro Minute erzeugen.
        _configure_logging(None)
        logging.getLogger("scheduler_cli").info(
            "Keine fälligen Pairs (%d konfiguriert)", len(status)
        )
        return 0

    rc = 0

    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
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
                job_id = db.job_start("backup", log_file=str(log_file))
                try:
                    summary = run_job(
                        dry_run=False, pairs_filter=due, trigger="scheduler"
                    )
                    status_name = "ok" if summary.get("ok") else "error"
                    db.job_finish(job_id, status_name, summary)
                    logger.info(
                        "Scheduler-Run fertig: %s",
                        json.dumps(summary, ensure_ascii=False, default=str)[:1000],
                    )
                    if status_name != "ok":
                        rc = 1
                except Exception as e:
                    logger.exception("Scheduler-Run gescheitert: %s", e)
                    db.job_finish(job_id, "error", {"error": str(e), "due": due})
                    rc = 1

    if pbs_due:
        logger.info("Fällige PBS-Targets: %s", pbs_due)
        with file_lock_or_none("pbs") as got_lock:
            if got_lock is None:
                logger.warning("PBS-Backup läuft bereits - überspringe diesen Tick")
            else:
                job_id = db.job_start("pbs", log_file=str(log_file))
                try:
                    summary = run_pbs_backup(pbs_due, trigger="scheduler")
                    status_name = "ok" if summary.get("ok") else "error"
                    db.job_finish(job_id, status_name, summary)
                    if status_name != "ok":
                        rc = 1
                except Exception as e:
                    logger.exception("PBS-Scheduler-Run gescheitert: %s", e)
                    db.job_finish(job_id, "error", {"error": str(e), "due": pbs_due})
                    rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
