import os
import time
from pathlib import Path

import pytest

from app import maintenance


def test_log_pruning_stays_inside_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "logs"
    root.mkdir()
    old = root / "old.log"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    try:
        (root / "link.log").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks require Windows Developer Mode or elevated privileges")
    monkeypatch.setattr(maintenance, "logs_root", lambda: root.resolve())

    result = maintenance.prune_logs(days=5, dry_run=False)
    assert result["deleted"] == 1
    assert not old.exists()
    assert outside.exists()


def test_automatic_maintenance_prunes_push_state(monkeypatch):
    class Config:
        def get(self, *_keys, default=None):
            return default

    class Database:
        def __init__(self):
            self.calls = []

        def jobs_prune(self, days, keep):
            self.calls.append(("jobs", days, keep))
            return 1

        def auth_prune(self, days):
            self.calls.append(("auth", days))
            return 2

        def audit_prune(self, days, keep):
            self.calls.append(("audit", days, keep))
            return 3

        def push_device_prune_expired(self):
            self.calls.append(("push_devices",))
            return 4

        def push_outbox_prune(self, *, older_than_days):
            self.calls.append(("push_outbox", older_than_days))
            return 5

        def checkpoint(self):
            self.calls.append(("checkpoint",))

    database = Database()
    monkeypatch.setattr(maintenance, "get_config", lambda: Config())
    monkeypatch.setattr(maintenance, "get_db", lambda: database)
    monkeypatch.setattr(
        maintenance,
        "prune_logs",
        lambda **_kwargs: {"deleted": 6, "bytes_deleted": 7},
    )

    result = maintenance.run_automatic_maintenance()

    assert result["deleted_push_devices"] == 4
    assert result["deleted_push_outbox"] == 5
    assert ("push_outbox", 30) in database.calls
    assert database.calls[-1] == ("checkpoint",)
