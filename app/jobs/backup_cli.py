"""CLI-Wrapper für den Backup-Job (von systemd aufgerufen)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from ..config_store import get_config
from ..db import get_db
from .locks import file_lock_or_none
from .rclone_sync import run_job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log_dir = Path(get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"backup-{datetime.now():%Y%m%d-%H%M%S}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("backup_cli")

    with file_lock_or_none("backup") as flock:
        if flock is None:
            logger.info("Backup-Job (CLI): anderer Prozess hält den Lock - skip")
            return 0

        db = get_db()
        job_id = db.job_start("backup", log_file=str(log_file))
        logger.info(f"Backup-Job gestartet (ID={job_id}, via CLI, dry_run={args.dry_run})")

        try:
            summary = run_job(dry_run=args.dry_run)
            db.job_finish(job_id, "ok", summary)
            logger.info(f"OK: {json.dumps(summary, ensure_ascii=False, default=str)[:500]}")
            return 0
        except Exception as e:
            logger.exception("Backup-Job fehlgeschlagen")
            db.job_finish(job_id, "error", {"error": str(e)})
            return 1


if __name__ == "__main__":
    sys.exit(main())
