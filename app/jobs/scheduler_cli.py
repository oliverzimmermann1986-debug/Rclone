"""CLI: minütlich aufgerufen, triggert fällige Pairs.

Wird von systemd-Timer scrapper-scheduler.timer aufgerufen.
Liest Per-Pair-Schedules und syncht nur was jetzt dran ist.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from ..config_store import get_config
from ..db import get_db
from .locks import file_lock_or_none
from .rclone_sync import run_job
from .scheduler import find_due_pairs


def main() -> int:
    cfg = get_config()
    db = get_db()

    due, status = find_due_pairs(cfg, db)

    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"scheduler-{datetime.now():%Y%m%d-%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )
    logger = logging.getLogger("scheduler_cli")

    if not due:
        # Kein Pair fällig - das ist der Normalfall, fast jede Minute
        logger.info("Keine fälligen Pairs (%d konfiguriert)", len(status))
        return 0

    logger.info("Fällige Pairs: %s", due)

    # File-Lock damit nicht zwei Sync-Aufrufe parallel laufen
    lock_path = Path("/tmp/scrapper-backup.lock")
    with file_lock_or_none(lock_path) as got_lock:
        if not got_lock:
            logger.warning("Sync läuft bereits - überspringe diesen Tick")
            return 0
        try:
            summary = run_job(dry_run=False, pairs_filter=due)
            logger.info("Scheduler-Run fertig: %s", summary)
        except Exception as e:
            logger.exception("Scheduler-Run gescheitert: %s", e)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
