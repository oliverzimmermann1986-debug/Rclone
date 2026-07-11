import os
from pathlib import Path

from app.jobs import locks


def test_contending_lock_does_not_truncate_owner_pid(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path)
    lock_path = tmp_path / "backup.lock"

    with locks.file_lock_or_none("backup") as first:
        assert first is not None
        expected = f"{os.getpid()}\n"
        assert lock_path.read_text(encoding="utf-8") == expected

        with locks.file_lock_or_none("backup") as second:
            assert second is None
        assert lock_path.read_text(encoding="utf-8") == expected


def test_symlink_lock_path_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("do-not-touch", encoding="utf-8")
    (tmp_path / "backup.lock").symlink_to(target)

    with locks.file_lock_or_none("backup") as handle:
        assert handle is None
    assert target.read_text(encoding="utf-8") == "do-not-touch"
