"""Regression tests for concurrent per-job log isolation."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from app.jobs import scheduler_cli
from app.routes import api_jobs


class _LogConfig:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir

    def get(self, *keys, default=None):
        if keys == ("paths", "logs_dir"):
            return str(self.log_dir)
        return default


def _emit_parallel() -> None:
    barrier = threading.Barrier(2)

    def emit(logger_name: str, message: str) -> None:
        barrier.wait(timeout=2)
        for _ in range(50):
            logging.getLogger(logger_name).warning(message)

    backup = threading.Thread(
        target=emit,
        args=("app.jobs.rclone_sync", "BACKUP /mnt/private-source"),
    )
    pbs = threading.Thread(
        target=emit,
        args=("app.jobs.pbs_backup", "PBS datastore:secret-target"),
    )
    backup.start()
    pbs.start()
    backup.join(timeout=2)
    pbs.join(timeout=2)
    assert not backup.is_alive() and not pbs.is_alive()


def _assert_isolated(backup_path: Path, pbs_path: Path) -> None:
    backup_text = backup_path.read_text(encoding="utf-8")
    pbs_text = pbs_path.read_text(encoding="utf-8")
    assert "BACKUP /mnt/private-source" in backup_text
    assert "PBS datastore:secret-target" not in backup_text
    assert "PBS datastore:secret-target" in pbs_text
    assert "BACKUP /mnt/private-source" not in pbs_text


def test_scheduler_job_handlers_isolate_parallel_scopes(tmp_path: Path):
    backup_path = scheduler_cli._job_log_file(tmp_path, "backup", "daily")
    pbs_path = scheduler_cli._job_log_file(tmp_path, "pbs", "archive")
    backup_handler = scheduler_cli._attach_job_log(backup_path, "backup")
    pbs_handler = scheduler_cli._attach_job_log(pbs_path, "pbs")
    try:
        _emit_parallel()
    finally:
        scheduler_cli._detach_job_log(backup_handler)
        scheduler_cli._detach_job_log(pbs_handler)

    _assert_isolated(backup_path, pbs_path)


def test_web_job_handlers_isolate_parallel_scopes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_jobs, "get_config", lambda: _LogConfig(tmp_path))
    backup_path, backup_handler = api_jobs._setup_job_logger(1, "backup")
    pbs_path, pbs_handler = api_jobs._setup_job_logger(2, "pbs")
    try:
        _emit_parallel()
    finally:
        api_jobs._remove_job_logger(backup_handler)
        api_jobs._remove_job_logger(pbs_handler)

    _assert_isolated(backup_path, pbs_path)
