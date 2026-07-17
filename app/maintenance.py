"""Gemeinsame, sichere Aufbewahrungs- und Log-Wartung."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

from .config_store import get_config
from .db import get_db
from .security import is_relative_to
from .utils import bounded_int as _bounded_int




def logs_root() -> Path:
    return Path(
        get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")
    ).resolve()


def iter_logs(root: Path | None = None) -> Iterator[Path]:
    base = (root or logs_root()).resolve()
    if not base.is_dir():
        return
    for path in base.rglob("*.log"):
        try:
            resolved = path.resolve()
            if resolved.is_file() and is_relative_to(resolved, base):
                yield resolved
        except (OSError, RuntimeError):
            continue


def prune_logs(*, days: int, dry_run: bool, limit_details: int = 200) -> dict[str, Any]:
    root = logs_root()
    cutoff = time.time() - max(1, int(days)) * 86400
    candidates: list[dict[str, Any]] = []
    matched = deleted = bytes_deleted = 0
    for path in iter_logs(root):
        try:
            stat_result = path.stat()
            if stat_result.st_mtime >= cutoff:
                continue
            matched += 1
            if len(candidates) < max(0, limit_details):
                candidates.append(
                    {
                        "path": str(path.relative_to(root)),
                        "size": stat_result.st_size,
                        "mtime": stat_result.st_mtime,
                    }
                )
            if not dry_run:
                path.unlink()
                deleted += 1
                bytes_deleted += stat_result.st_size
        except OSError:
            continue
    return {
        "ok": True,
        "dry_run": dry_run,
        "days": days,
        "matched": matched,
        "deleted": deleted,
        "bytes_deleted": bytes_deleted,
        "files": candidates,
        "truncated": matched > len(candidates),
    }


def run_automatic_maintenance() -> dict[str, Any]:
    settings = get_config().get("maintenance", default={}) or {}
    if not settings.get("auto_prune", True):
        return {"enabled": False}
    retention_days = _bounded_int(
        settings.get("job_retention_days", 180), default=180, minimum=1, maximum=3650
    )
    keep_latest = _bounded_int(
        settings.get("keep_latest_jobs", 500), default=500, minimum=10, maximum=100000
    )
    log_days = _bounded_int(
        settings.get("log_retention_days", 90), default=90, minimum=1, maximum=3650
    )
    deleted_jobs = get_db().jobs_prune(retention_days, keep_latest)
    deleted_auth = get_db().auth_prune(7)
    deleted_audit = get_db().audit_prune(
        max(retention_days, 365), max(keep_latest, 1000)
    )
    logs = prune_logs(days=log_days, dry_run=False, limit_details=0)
    get_db().checkpoint()
    return {
        "enabled": True,
        "deleted_jobs": deleted_jobs,
        "deleted_auth_rows": deleted_auth,
        "deleted_audit_events": deleted_audit,
        "deleted_logs": logs["deleted"],
        "deleted_log_bytes": logs["bytes_deleted"],
    }
