"""Prozessübergreifende File-Locks für Web-Trigger, CLI und Scheduler."""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Optional, Union

from ..file_lock import acquire as acquire_file_lock
from ..file_lock import release as release_file_lock

logger = logging.getLogger(__name__)

LOCK_DIR = Path(os.getenv("RCLONE_SYNC_LOCK_DIR", "/opt/rclone-sync/data/locks"))


class HeldFileLock:
    """Übertragbare, idempotent lösbare Prozess-Lock-Lease.

    Web-Requests erwerben die Lease vor der DB-Reservation und geben sie im
    Worker-Thread wieder frei. Dadurch gibt es kein sichtbares ``running``-Fenster
    ohne den zugehörigen prozessübergreifenden Lock.
    """

    def __init__(self, handle: IO[str]):
        self.handle = handle
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            try:
                release_file_lock(self.handle.fileno())
            except OSError:
                pass
            try:
                self.handle.close()
            except OSError:
                pass

    def __enter__(self) -> IO[str]:
        return self.handle

    def __exit__(self, *_exc_info) -> None:
        self.release()


def _safe_lock_name(name: Union[str, Path]) -> str:
    raw = str(name).strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "default"


def _open_lock_file(path: Path) -> IO[str]:
    """Öffnet eine normale Lockdatei ohne Symlinks und ohne Vorab-Truncate."""
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("Lockpfad ist keine reguläre Datei")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def try_file_lock(name: Union[str, Path]) -> Optional[HeldFileLock]:
    """Erwirbt non-blocking eine übertragbare Prozess-Lock-Lease."""

    LOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(LOCK_DIR, 0o700)
    except OSError:
        pass
    safe_name = _safe_lock_name(name)
    lock_path = LOCK_DIR / f"{safe_name}.lock"
    fh: Optional[IO[str]] = None
    try:
        try:
            fh = _open_lock_file(lock_path)
        except OSError as exc:
            logger.error(
                "file_lock %r konnte nicht sicher geöffnet werden: %s", safe_name, exc
            )
            return None

        try:
            acquire_file_lock(fh.fileno(), blocking=False)
            fh.seek(0)
            fh.write(f"{os.getpid()}\n")
            fh.truncate()
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
            lease = HeldFileLock(fh)
            fh = None
            return lease
        except BlockingIOError:
            try:
                fh.seek(0)
                other_pid = fh.read(64).strip()
            except (OSError, UnicodeError):
                other_pid = "?"
            logger.info(
                "file_lock %r: gehalten von PID %s", safe_name, other_pid or "?"
            )
            return None
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


@contextmanager
def file_lock_or_none(name: Union[str, Path]) -> Iterator[Optional[IO[str]]]:
    """Versucht non-blocking eine Datei zu locken.

    Der Inhalt wird erst *nach* erfolgreichem ``flock`` ersetzt. Dadurch kann ein
    konkurrierender Prozess die Besitzer-PID nicht versehentlich leeren. Unsichere
    Lockpfade (z. B. Symlinks) führen fail-closed zu ``None``.
    """

    lease = try_file_lock(name)
    try:
        yield lease.handle if lease is not None else None
    finally:
        if lease is not None:
            lease.release()


def is_locked(name: Union[str, Path]) -> bool:
    """Prüft, ob der Lock aktuell belegt oder nicht sicher zugänglich ist."""
    with file_lock_or_none(name) as fh:
        return fh is None
