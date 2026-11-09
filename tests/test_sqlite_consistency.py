from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app import sqlite_consistency


def _create_sqlite_db(path: Path, rows: list[tuple[int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, value TEXT)"
        )
        conn.executemany("INSERT INTO items (id, value) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _read_sqlite_rows(path: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute("SELECT id, value FROM items ORDER BY id")
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]
    finally:
        conn.close()


def test_is_sqlite_file_detection(tmp_path):
    valid_db = tmp_path / "app.db"
    _create_sqlite_db(valid_db, [(1, "alpha")])
    assert sqlite_consistency.is_sqlite_file(valid_db) is True

    valid_sqlite3 = tmp_path / "data.SQLITE3"
    _create_sqlite_db(valid_sqlite3, [(2, "beta")])
    assert sqlite_consistency.is_sqlite_file(valid_sqlite3) is True

    # Non-SQLite file with .db extension
    fake_db = tmp_path / "fake.db"
    fake_db.write_bytes(b"not a sqlite database file at all")
    assert sqlite_consistency.is_sqlite_file(fake_db) is False

    # Empty .db file
    empty_db = tmp_path / "empty.db"
    empty_db.write_bytes(b"")
    assert sqlite_consistency.is_sqlite_file(empty_db) is False

    # SQLite header but wrong extension
    wrong_ext = tmp_path / "notes.txt"
    wrong_ext.write_bytes(sqlite_consistency.SQLITE_HEADER + b"extra")
    assert sqlite_consistency.is_sqlite_file(wrong_ext) is False

    # Symlink to valid SQLite DB must be ignored
    link_db = tmp_path / "link.db"
    try:
        os.symlink(valid_db, link_db)
        has_symlink = True
    except (OSError, NotImplementedError):
        has_symlink = False
    if has_symlink:
        assert sqlite_consistency.is_sqlite_file(link_db) is False


def test_backup_sqlite_directory_and_stale_cleanup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    root_db = source / "main.sqlite"
    nested_db = source / "sub" / "nested.db"
    _create_sqlite_db(root_db, [(1, "one"), (2, "two")])
    _create_sqlite_db(nested_db, [(10, "ten")])

    # Non-SQLite .db file and regular text file should be ignored
    (source / "other.db").write_text("plain text", encoding="utf-8")
    (source / "readme.md").write_text("# hello", encoding="utf-8")

    # Ignored directory .rclone-versions with a SQLite DB inside must be skipped
    ignored_db = source / ".rclone-versions" / "archived.db"
    _create_sqlite_db(ignored_db, [(99, "ignored")])

    result = sqlite_consistency.backup_sqlite_directory(str(source))
    assert result["active"] is True
    assert result["snapshot_dir"] == sqlite_consistency.SNAPSHOT_DIR_NAME
    assert result["databases"] == 2
    assert sorted(result["files"]) == ["main.sqlite", "sub/nested.db"]
    assert result["failed"] == []
    assert result["bytes"] > 0

    snap_root = source / sqlite_consistency.SNAPSHOT_DIR_NAME
    snap_main = snap_root / "main.sqlite"
    snap_nested = snap_root / "sub" / "nested.db"
    assert snap_main.is_file()
    assert snap_nested.is_file()
    assert _read_sqlite_rows(snap_main) == [(1, "one"), (2, "two")]
    assert _read_sqlite_rows(snap_nested) == [(10, "ten")]

    # Remove nested_db and update root_db -> stale snapshot must be cleaned up
    nested_db.unlink()
    _create_sqlite_db(root_db, [(3, "three")])

    second = sqlite_consistency.backup_sqlite_directory(str(source))
    assert second["databases"] == 1
    assert second["files"] == ["main.sqlite"]
    assert "sub/nested.db" in second["stale_removed"]
    assert not snap_nested.exists()
    assert _read_sqlite_rows(snap_main) == [(1, "one"), (2, "two"), (3, "three")]


def test_sqlite_consistent_source_context_manager(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    _create_sqlite_db(source / "vault.sqlite3", [(7, "seven")])

    # Disabled when sqlite_backup is False or omitted
    with sqlite_consistency.sqlite_consistent_source(
        {"name": "p1", "local": str(source), "direction": "push"}
    ) as info:
        assert info["active"] is False
        assert info["databases"] == 0

    # Enabled for push/bisync
    with sqlite_consistency.sqlite_consistent_source(
        {
            "name": "p1",
            "local": str(source),
            "direction": "push",
            "sqlite_backup": True,
        }
    ) as info:
        assert info["active"] is True
        assert info["databases"] == 1
        assert info["files"] == ["vault.sqlite3"]

    # Rejects pull direction
    with pytest.raises(ValueError, match="push"):
        with sqlite_consistency.sqlite_consistent_source(
            {
                "name": "p1",
                "local": str(source),
                "direction": "pull",
                "sqlite_backup": True,
            }
        ):
            pass

    # Rejects remote path as local
    with pytest.raises(ValueError):
        with sqlite_consistency.sqlite_consistent_source(
            {
                "name": "p1",
                "local": "remote:bucket",
                "direction": "push",
                "sqlite_backup": True,
            }
        ):
            pass
