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
from . import runtime_state
from .job_lifecycle import BACKUP_KINDS, reconcile_locked_scope
from .locks import file_lock_or_none
from .rclone_sync import reset_cancel, run_job
from .scheduler import rclone_history_key


def _job_status(summary: dict) -> str:
    if summary.get("cancelled") is True:
        return "cancelled"
    pairs = summary.get("pairs") or []
    if any(isinstance(pair, dict) and pair.get("cancelled") is True for pair in pairs):
        return "cancelled"
    return "ok" if summary.get("ok") else "error"


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pairs", help="Kommagetrennte Pair-Namen, z.B. Serien,Filme")
    args = parser.parse_args()

    cfg = get_config()
    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs"))
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
    backup = cfg.get("backup", default={}) or {}
    wanted = set(pairs_filter or [])
    configured_pairs = [
        pair
        for pair in (backup.get("pairs") or [])
        if isinstance(pair, dict)
        and pair.get("enabled", True)
        and (not wanted or str(pair.get("name") or "") in wanted)
    ]
    history_keys = {
        str(pair.get("name")): rclone_history_key(pair) for pair in configured_pairs
    }
    attempts = [
        {
            "name": name,
            "history_key": history_key,
            "trigger": "cli",
            "dry_run": args.dry_run,
        }
        for name, history_key in history_keys.items()
    ]

    with file_lock_or_none("backup") as flock:
        if flock is None:
            logger.info("Backup-Job (CLI): anderer Prozess hält den Lock - skip")
            return 0

        db = get_db()
        reconciliation = reconcile_locked_scope(
            db,
            scope=runtime_state.DEFAULT_CANCEL_SCOPE,
            kinds=BACKUP_KINDS,
        )
        if not reconciliation.get("safe"):
            logger.error("Registrierter Sync-Unterprozess ist noch aktiv; Abbruch")
            return 1
        reset_cancel()
        job_id = db.job_start(
            "backup",
            log_file=str(log_file),
            attempts=attempts,
            exclusive_scope=True,
        )
        logger.info(
            "Backup-Job gestartet (ID=%s, via CLI, dry_run=%s, pairs=%s)",
            job_id,
            args.dry_run,
            pairs_filter,
        )

        try:
            summary = run_job(
                dry_run=args.dry_run,
                pairs_filter=pairs_filter,
                trigger="cli",
                job_id=job_id,
                defer_runtime_finish=True,
                reset_cancel_state=False,
            )
            summary = dict(summary)
            summary.setdefault("trigger", "cli")
            summary.setdefault("dry_run", args.dry_run)
            summary["history_keys"] = history_keys
            status = _job_status(summary)
            transitioned = db.job_finish(job_id, status, summary)
            if transitioned:
                _finish_runtime_for_job(job_id, status)
            else:
                actual = db.job_get(job_id) or {}
                _finish_runtime_for_job(job_id, str(actual.get("status") or "stale"))
            logger.info(
                "Fertig: %s",
                json.dumps(summary, ensure_ascii=False, default=str)[:1000],
            )
            return 0 if status == "ok" else 1
        except Exception as e:
            logger.exception("Backup-Job fehlgeschlagen")
            transitioned = db.job_finish(
                job_id,
                "error",
                {
                    "ok": False,
                    "error": str(e),
                    "due": list(history_keys),
                    "trigger": "cli",
                    "dry_run": args.dry_run,
                    "history_keys": history_keys,
                },
            )
            if transitioned:
                _finish_runtime_for_job(job_id, "error", error=str(e))
            else:
                actual = db.job_get(job_id) or {}
                _finish_runtime_for_job(
                    job_id, str(actual.get("status") or "stale"), error=str(e)
                )
            return 1


if __name__ == "__main__":
    sys.exit(main())
