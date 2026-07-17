"""CLI-Wrapper für Backup-Jobs (von systemd oder manuell aufgerufen)."""

from __future__ import annotations

import argparse
import json
import logging
import os
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
    parser.add_argument("--pairs", help="Kommagetrennte Pair-Namen, z.B. Serien,Filme")
    args = parser.parse_args()

    log_dir = Path(
        get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"backup-{datetime.now():%Y%m%d-%H%M%S}.log"

    try:
        log_file.touch(mode=0o600, exist_ok=True)
        os.chmod(log_file, 0o600)
    except OSError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger = logging.getLogger("backup_cli")

    pairs_filter = (
        [p.strip() for p in args.pairs.split(",") if p.strip()] if args.pairs else None
    )

    with file_lock_or_none("backup") as flock:
        if flock is None:
            logger.info("Backup-Job (CLI): anderer Prozess hält den Lock - skip")
            return 0

        db = get_db()
        job_id = db.job_start("backup", log_file=str(log_file))
        logger.info(
            "Backup-Job gestartet (ID=%s, via CLI, dry_run=%s, pairs=%s)",
            job_id,
            args.dry_run,
            pairs_filter,
        )

        try:
            summary = run_job(
                dry_run=args.dry_run, pairs_filter=pairs_filter, trigger="cli"
            )
            status = "ok" if summary.get("ok") else "error"
            db.job_finish(job_id, status, summary)
            logger.info(
                "Fertig: %s",
                json.dumps(summary, ensure_ascii=False, default=str)[:1000],
            )
            return 0 if status == "ok" else 1
        except Exception as e:
            logger.exception("Backup-Job fehlgeschlagen")
            db.job_finish(job_id, "error", {"error": str(e)})
            return 1


if __name__ == "__main__":
    sys.exit(main())
