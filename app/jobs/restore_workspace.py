"""Owned restore-drill scratch directories that survive a process restart."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

from ..file_lock import acquire
from .locks import HeldFileLock

logger = logging.getLogger(__name__)
REGISTRY = ".restore-workspaces"
KINDS = {"restore-drill", "vault-rescue", "snapshot-capture"}


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _try_lock(root: Path, name: str) -> HeldFileLock | None:
    locks = root / ".restore-workspace-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.is_symlink():
        raise OSError("Workspace lock directory is a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(locks / f"{name}.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Workspace lock is not a regular file")
        try:
            acquire(fd, blocking=False)
        except BlockingIOError:
            return None
        handle = os.fdopen(fd, "r+", encoding="utf-8")
        fd = -1
        return HeldFileLock(handle)
    finally:
        if fd >= 0:
            os.close(fd)


def _record_path(workdir: Path) -> Path:
    return workdir.parent / REGISTRY / f"{workdir.name}.json"


def forget_workspace(workdir: Path) -> None:
    if not workdir.exists():
        _record_path(workdir).unlink(missing_ok=True)


def create_workspace(
    root: Path, prefix: str, *, kind: str = "restore-drill"
) -> tuple[Path, HeldFileLock]:
    if not prefix.startswith("restore-") or kind not in KINDS:
        raise ValueError("Unsupported restore workspace")
    root.mkdir(parents=True, exist_ok=True)
    registry = root / REGISTRY
    registry.mkdir(mode=0o700, exist_ok=True)
    if registry.is_symlink():
        raise OSError("Workspace registry is a symlink")
    workdir = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    lease = None
    try:
        lease = _try_lock(root, workdir.name)
        if lease is None:
            raise OSError("New restore workspace is already locked")
        marker = {"version": 1, "kind": kind, "name": workdir.name}
        with _record_path(workdir).open("x", encoding="utf-8") as handle:
            json.dump(marker, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(registry)
        _sync_directory(root)
        return workdir, lease
    except Exception:
        try:
            shutil.rmtree(workdir)
            forget_workspace(workdir)
        finally:
            if lease is not None:
                lease.release()
        raise


def cleanup_orphaned_workspaces(root: Path) -> dict[str, int]:
    """Called with backup scope held and registered subprocesses confirmed gone.

    Never delete unregistered legacy directories or follow directory/record links.
    A per-workspace lease also protects a directly running drill.
    """
    result = {"removed": 0, "failed": 0}
    if not root.is_dir():
        return result
    registry = root / REGISTRY
    if not registry.is_dir() or registry.is_symlink():
        return result
    for marker_path in registry.glob("restore-*.json"):
        workdir = root / marker_path.stem
        if marker_path.is_symlink():
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or marker.get("kind") not in KINDS:
                continue
            if marker != {"version": 1, "kind": marker["kind"], "name": workdir.name}:
                continue
            if workdir.is_symlink():
                continue
            lease = _try_lock(root, workdir.name)
            if lease is None:
                continue
            try:
                if workdir.exists():
                    shutil.rmtree(workdir)
                    result["removed"] += 1
                forget_workspace(workdir)
            finally:
                lease.release()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            result["failed"] += 1
            logger.exception(
                "Orphaned restore workspace could not be removed: %s", workdir
            )
    return result
