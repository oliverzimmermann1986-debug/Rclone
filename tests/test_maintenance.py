import os
import time
from pathlib import Path

from app import maintenance


def test_log_pruning_stays_inside_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "logs"
    root.mkdir()
    old = root / "old.log"
    old.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    outside = tmp_path / "outside.log"
    outside.write_text("secret", encoding="utf-8")
    (root / "link.log").symlink_to(outside)
    monkeypatch.setattr(maintenance, "logs_root", lambda: root.resolve())

    result = maintenance.prune_logs(days=5, dry_run=False)
    assert result["deleted"] == 1
    assert not old.exists()
    assert outside.exists()
