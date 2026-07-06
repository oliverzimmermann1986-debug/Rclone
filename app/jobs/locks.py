"""Prozessübergreifende File-Locks für Web-Trigger, CLI und Scheduler."""
from __future__ import annotations

import fcntl
import logging
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

logger = logging.getLogger(__name__)

LOCK_DIR = Path(os.getenv("RCLONE_SYNC_LOCK_DIR", "/opt/rclone-sync/data/locks"))


def _safe_lock_name(name: Union[str, Path]) -> str:
    raw = str(name).strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._") or "default"


@contextmanager
def file_lock_or_none(name: Union[str, Path]) -> Iterator[Optional[object]]:
    """Versucht non-blocking eine Datei zu locken.

    Yields:
        File-Handle wenn der Lock erworben wurde, sonst ``None``.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_lock_name(name)
    lock_path = LOCK_DIR / f"{safe_name}.lock"
    fh = None
    acquired = False
    try:
        fh = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield fh
        except BlockingIOError:
            try:
                other_pid = lock_path.read_text(encoding="utf-8").strip()
            except Exception:
                other_pid = "?"
            logger.info("file_lock %r: gehalten von PID %s", safe_name, other_pid)
            yield None
    finally:
        if fh is not None:
            if acquired:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                fh.close()
            except Exception:
                pass


def is_locked(name: Union[str, Path]) -> bool:
    """Prüft, ob der Lock aktuell belegt ist."""
    with file_lock_or_none(name) as fh:
        return fh is None
