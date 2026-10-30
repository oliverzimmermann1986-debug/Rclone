"""Independent minute tick for heartbeat, capacity observations and reports."""

from __future__ import annotations

import logging

from . import heartbeat, protection_reports, storage_forecast
from .config_store import get_config
from .db import get_db
from .jobs.locks import try_file_lock
from .lockdown import is_locked_down

logger = logging.getLogger(__name__)


def run_once(cfg=None, db=None) -> int:
    cfg = cfg if cfg is not None else get_config()
    db = db if db is not None else get_db()
    failed = False
    # First: a slow storage provider must not postpone the current heartbeat.
    actions = [("Lebenszeichen", heartbeat.tick)]
    if not is_locked_down(db):
        actions.extend(
            [
                ("Speichermessung", storage_forecast.sample_if_due),
                ("Schutzbericht", protection_reports.tick),
            ]
        )
    for label, action in actions:
        try:
            action(cfg, db)
        except Exception as exc:
            # URLs and SMTP exceptions can contain credentials. Record only type.
            logger.warning("%s nicht abgeschlossen (%s)", label, type(exc).__name__)
            failed = True
    return int(failed)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    lease = try_file_lock("operations")
    if lease is None:
        return 0
    try:
        return run_once()
    finally:
        lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
