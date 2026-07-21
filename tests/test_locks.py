import os
from pathlib import Path

import pytest

from app.jobs import locks


def test_contending_lock_does_not_truncate_owner_pid(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path)
    lock_path = tmp_path / "backup.lock"

    with locks.file_lock_or_none("backup") as first:
        assert first is not None
        expected = f"{os.getpid()}\n"
        if os.name == "nt":
            first.seek(0)
            assert first.read() == expected
        else:
            assert lock_path.read_text(encoding="utf-8") == expected

        with locks.file_lock_or_none("backup") as second:
            assert second is None
        if os.name != "nt":
            assert lock_path.read_text(encoding="utf-8") == expected


def test_symlink_lock_path_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("do-not-touch", encoding="utf-8")
    try:
        (tmp_path / "backup.lock").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks require Windows Developer Mode or elevated privileges")

    with locks.file_lock_or_none("backup") as handle:
        assert handle is None
    assert target.read_text(encoding="utf-8") == "do-not-touch"
