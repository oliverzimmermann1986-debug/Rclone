"""Prozessübergreifende File-Locks für Web-Trigger, CLI und Scheduler."""

from __future__ import annotations

import fcntl
import logging
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Optional, Union

logger = logging.getLogger(__name__)

LOCK_DIR = Path(os.getenv("RCLONE_SYNC_LOCK_DIR", "/opt/rclone-sync/data/locks"))


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


@contextmanager
def file_lock_or_none(name: Union[str, Path]) -> Iterator[Optional[IO[str]]]:
    """Versucht non-blocking eine Datei zu locken.

    Der Inhalt wird erst *nach* erfolgreichem ``flock`` ersetzt. Dadurch kann ein
    konkurrierender Prozess die Besitzer-PID nicht versehentlich leeren. Unsichere
    Lockpfade (z. B. Symlinks) führen fail-closed zu ``None``.

    Yields:
        File-Handle wenn der Lock erworben wurde, sonst ``None``.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(LOCK_DIR, 0o700)
    except OSError:
        pass
    safe_name = _safe_lock_name(name)
    lock_path = LOCK_DIR / f"{safe_name}.lock"
    fh: Optional[IO[str]] = None
    acquired = False
    try:
        try:
            fh = _open_lock_file(lock_path)
        except OSError as exc:
            logger.error(
                "file_lock %r konnte nicht sicher geöffnet werden: %s", safe_name, exc
            )
            yield None
            return

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
            yield fh
        except BlockingIOError:
            try:
                fh.seek(0)
                other_pid = fh.read(64).strip()
            except (OSError, UnicodeError):
                other_pid = "?"
            logger.info(
                "file_lock %r: gehalten von PID %s", safe_name, other_pid or "?"
            )
            yield None
    finally:
        if fh is not None:
            if acquired:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass


def is_locked(name: Union[str, Path]) -> bool:
    """Prüft, ob der Lock aktuell belegt oder nicht sicher zugänglich ist."""
    with file_lock_or_none(name) as fh:
        return fh is None
