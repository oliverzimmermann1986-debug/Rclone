"""Scope filters for per-job log files."""

from __future__ import annotations

import logging

_LOGGER_PREFIXES: dict[str, tuple[str, ...]] = {
    "backup": ("app.jobs.rclone_sync",),
    "check": ("app.jobs.rclone_sync",),
    "quicksync": ("app.jobs.rclone_sync",),
    "pbs": ("app.jobs.pbs_backup",),
    "restoretest": ("app.jobs.restore_test",),
}


class JobScopeFilter(logging.Filter):
    """Accept records produced by exactly one durable job scope."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind = str(kind or "").strip().lower()
        self.prefixes = _LOGGER_PREFIXES.get(
            self.kind, (f"app.jobs.{self.kind}",) if self.kind else ()
        )

    def filter(self, record: logging.LogRecord) -> bool:
        explicit_scope = str(getattr(record, "job_log_scope", "") or "").lower()
        if explicit_scope:
            return explicit_scope == self.kind
        return any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in self.prefixes
        )
