"""File-basierte Locks zwischen Web-Trigger und systemd-CLI.

Der In-Process ``threading.Lock`` in api_jobs schützt nur den Web-Prozess.
Wenn der ``rclone-sync.timer`` feuert während ein Web-Trigger schon läuft,
würden ZWEI Scraper-Prozesse gleichzeitig E-Mails lesen und Videos laden -
plus die History/Pending-DB beschreiben. Dieses Modul schließt die Lücke
per ``fcntl.flock``, das auch über Prozessgrenzen hinweg greift.

Verwendung:
    with file_lock_or_none("scraper") as fh:
        if fh is None:
            # anderer Prozess hält den Lock - sauber rausgehen
            return
        # ... Job-Arbeit ...

Der Lock wird beim Verlassen des with-Blocks automatisch freigegeben,
auch bei Exception oder ``os._exit``.
"""
from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

LOCK_DIR = Path("/opt/rclone-sync/data/locks")


@contextmanager
def file_lock_or_none(name: str) -> Iterator[Optional[object]]:
    """Versucht non-blocking eine Datei zu locken.

    Yields:
        Das geöffnete File-Handle wenn der Lock erworben wurde,
        sonst ``None`` (Caller MUSS checken).

    Der Lock-File-Pfad ist ``{LOCK_DIR}/{name}.lock``. Das File bleibt
    zwischen Runs liegen (nur die ``fcntl.flock``-Sperre ist transient).
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{name}.lock"
    fh = None
    acquired = False
    try:
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            # PID reinschreiben - rein informativ für Debugging
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield fh
        except BlockingIOError:
            # Lock ist von anderem Prozess gehalten
            try:
                other_pid = lock_path.read_text().strip()
            except Exception:
                other_pid = "?"
            logger.info(f"file_lock '{name}': gehalten von PID {other_pid}")
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


def is_locked(name: str) -> bool:
    """Probiert non-blocking, ob der Lock frei wäre. Gibt ihn sofort wieder ab.
    Achtung: zwischen Probe und Action-Aufruf kann sich der Status ändern.
    """
    with file_lock_or_none(name) as fh:
        return fh is None
