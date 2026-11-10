"""Konsistente Online-Vorabkopien lokaler SQLite-Datenbanken vor dem Sync."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .jobs.pair_planner import is_remote
from .recovery_walk import WalkLimitError, bounded_walk

logger = logging.getLogger(__name__)

SQLITE_HEADER = b"SQLite format 3\x00"
SNAPSHOT_DIR_NAME = ".sicherpfad-sqlite"
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
_MAX_SQLITE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_IGNORE_DIRS = {SNAPSHOT_DIR_NAME, ".rclone-versions", ".zfs"}


def is_sqlite_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    if path.suffix.lower() not in _SQLITE_SUFFIXES:
        return False
    if st.st_size <= 0 or st.st_size > _MAX_SQLITE_BYTES:
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError:
        return False
    return header == SQLITE_HEADER


def _ensure_safe_parent(snapshot_root: Path, rel_path: Path) -> Path:
    current = snapshot_root
    for part in rel_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"Symbolischer Link im Snapshot-Pfad verweigert: {current}"
            )
        current.mkdir(parents=True, exist_ok=True)
    target = current / rel_path.parts[-1]
    if target.is_symlink():
        raise ValueError(f"Symbolischer Link als Snapshot-Ziel verweigert: {target}")
    return target


def _backup_single_sqlite(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(str(tmp_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        try:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if dst.is_symlink():
            raise ValueError(f"Snapshot-Ziel ist ein symbolischer Link: {dst}")
        os.replace(tmp_path, dst)
        return int(dst.stat().st_size)
    finally:
        tmp_path.unlink(missing_ok=True)


def _cleanup_stale_snapshots(
    root: Path,
    snapshot_root: Path,
    active_rel_paths: set[str],
    *,
    max_scan_entries: int,
) -> list[str]:
    if not snapshot_root.exists() or snapshot_root.is_symlink():
        return []
    removed: list[str] = []
    visited_dirs: list[Path] = []
    try:
        for current, directories, filenames in bounded_walk(
            snapshot_root, max_entries=max(1, int(max_scan_entries))
        ):
            directories.sort()
            filenames.sort()
            if current != snapshot_root:
                visited_dirs.append(current)
            for filename in filenames:
                snap_file = current / filename
                try:
                    rel = snap_file.relative_to(snapshot_root)
                    rel_posix = rel.as_posix()
                except ValueError:
                    continue
                if rel_posix not in active_rel_paths or not is_sqlite_file(root / rel):
                    try:
                        snap_file.unlink(missing_ok=True)
                        removed.append(rel_posix)
                    except OSError:
                        logger.warning(
                            "Veraltete SQLite-Snapshot-Datei konnte nicht entfernt werden: %s",
                            snap_file,
                        )
    except WalkLimitError:
        pass

    for directory in sorted(visited_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass

    return removed


def backup_sqlite_directory(
    local_path: str, *, max_scan_entries: int = 25_000
) -> dict[str, Any]:
    cleaned = str(local_path or "").strip()
    if not cleaned or is_remote(cleaned):
        raise ValueError("SQLite-Backup erfordert einen lokalen Verzeichnispfad")
    root = Path(cleaned)
    if root.is_symlink() or not root.exists() or not root.is_dir():
        raise ValueError(f"Ungültiges lokales Quellverzeichnis: {local_path}")

    snapshot_root = root / SNAPSHOT_DIR_NAME
    if snapshot_root.is_symlink():
        raise ValueError(
            f"Snapshot-Verzeichnis darf kein symbolischer Link sein: {snapshot_root}"
        )

    backed_up: list[str] = []
    failed: list[dict[str, str]] = []
    total_bytes = 0
    truncated = False

    try:
        for current, directories, filenames in bounded_walk(
            root, max_entries=max(1, int(max_scan_entries))
        ):
            directories[:] = [d for d in directories if d not in _IGNORE_DIRS]
            directories.sort()
            filenames.sort()
            for filename in filenames:
                candidate = current / filename
                if not is_sqlite_file(candidate):
                    continue
                rel = candidate.relative_to(root)
                rel_posix = rel.as_posix()
                try:
                    snapshot_root.mkdir(parents=True, exist_ok=True)
                    dst = _ensure_safe_parent(snapshot_root, rel)
                    size = _backup_single_sqlite(candidate, dst)
                    backed_up.append(rel_posix)
                    total_bytes += size
                except (OSError, sqlite3.Error, ValueError) as exc:
                    logger.warning(
                        "SQLite-Backup für %s fehlgeschlagen: %s", rel_posix, exc
                    )
                    failed.append({"file": rel_posix, "error": str(exc)})
    except WalkLimitError:
        truncated = True

    active_set = set(backed_up) | {item["file"] for item in failed}
    stale_removed = _cleanup_stale_snapshots(
        root,
        snapshot_root,
        active_set,
        max_scan_entries=max_scan_entries,
    )

    return {
        "active": True,
        "snapshot_dir": SNAPSHOT_DIR_NAME,
        "databases": len(backed_up),
        "bytes": total_bytes,
        "files": backed_up,
        "failed": failed,
        "stale_removed": stale_removed,
        "truncated": truncated,
    }


@contextmanager
def sqlite_consistent_source(pair: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    if not isinstance(pair, Mapping) or pair.get("sqlite_backup") is not True:
        yield {
            "active": False,
            "databases": 0,
            "bytes": 0,
            "files": [],
            "failed": [],
        }
        return

    direction = str(pair.get("direction") or "push").strip().casefold()
    if direction not in {"push", "bisync"}:
        raise ValueError(
            "SQLite-Konsistenzsicherung wird nur für Richtung 'push' oder 'bisync' unterstützt"
        )

    local = str(pair.get("local") or "").strip()
    if not local or is_remote(local) or not Path(local).is_absolute():
        raise ValueError(
            "SQLite-Konsistenzsicherung erfordert einen absoluten lokalen Quellpfad"
        )

    result = backup_sqlite_directory(local)
    yield result


__all__ = [
    "SNAPSHOT_DIR_NAME",
    "SQLITE_HEADER",
    "backup_sqlite_directory",
    "is_sqlite_file",
    "sqlite_consistent_source",
]
