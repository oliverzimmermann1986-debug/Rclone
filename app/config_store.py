"""Prozess- und thread-sicherer, atomarer YAML-Konfigurationsspeicher."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import yaml

_CONFIG_PATH = Path(
    os.getenv("RCLONE_SYNC_CONFIG", "/opt/rclone-sync/data/config.yaml")
)


class ConfigConflictError(RuntimeError):
    """Die Konfiguration wurde seit dem Laden durch einen anderen Prozess geändert."""


class Config:
    """YAML-Konfiguration mit Reload-on-change, File-Lock und Revisionen.

    Atomare ``os.replace``-Schreibvorgänge verhindern halbe Dateien. Ein separater
    ``flock`` serialisiert zusätzlich Web, CLI und Scheduler-Prozesse. Revisionen
    ermöglichen der Web-UI, verlorene Updates frühzeitig mit HTTP 409 abzuweisen.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_name(f".{path.name}.lock")
        self._data: dict[str, Any] = {}
        self._mtime_ns: int = 0
        self._revision: str = "missing"
        self._dirty_base_revision: str | None = None
        self._lock = threading.RLock()
        self._load_unlocked()

    @contextmanager
    def _file_lock(self, *, exclusive: bool) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, stat.S_IRUSR | stat.S_IWUSR)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _hash_bytes(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def _load_unlocked(self) -> None:
        if self.path.exists():
            raw = self.path.read_bytes()
            loaded = yaml.safe_load(raw.decode("utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Config-Root muss ein Mapping sein: {self.path}")
            self._data = loaded
            self._revision = self._hash_bytes(raw)
            try:
                self._mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._mtime_ns = 0
        else:
            self._data = {}
            self._mtime_ns = 0
            self._revision = "missing"

    def _maybe_reload_unlocked(self) -> None:
        # Lokale set()-Änderungen dürfen nicht still durch einen Reload verworfen
        # werden. save() prüft anschließend die Basisrevision gegen die Platte.
        if self._dirty_base_revision is not None:
            return
        if not self.path.exists():
            if self._revision != "missing":
                self._load_unlocked()
            return
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            return
        if mtime_ns != self._mtime_ns:
            self._load_unlocked()

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            self._maybe_reload_unlocked()
            cur: Any = self._data
            for key in keys:
                if not isinstance(cur, dict) or key not in cur:
                    return default
                cur = cur[key]
            return copy.deepcopy(cur)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_reload_unlocked()
            return copy.deepcopy(self._data)

    def snapshot_with_revision(self) -> tuple[dict[str, Any], str]:
        with self._lock:
            self._maybe_reload_unlocked()
            return copy.deepcopy(self._data), self._revision

    @property
    def revision(self) -> str:
        with self._lock:
            self._maybe_reload_unlocked()
            return self._revision

    def set(self, *keys_and_value: Any) -> None:
        """Nur für lokale, unmittelbar folgende ``save``-Aufrufe.

        Prozessübergreifende Änderungen sollten immer ``update`` verwenden.
        """
        if len(keys_and_value) < 2:
            raise ValueError("set: mindestens key + value")
        *keys, value = keys_and_value
        with self._lock:
            self._maybe_reload_unlocked()
            if self._dirty_base_revision is None:
                self._dirty_base_revision = self._revision
            cur: dict[str, Any] = self._data
            for key in keys[:-1]:
                existing = cur.get(key)
                if existing is None:
                    existing = {}
                    cur[key] = existing
                if not isinstance(existing, dict):
                    raise ValueError(f"set: '{key}' ist kein Mapping")
                cur = existing
            cur[keys[-1]] = copy.deepcopy(value)

    def replace(
        self, data: dict[str, Any], *, expected_revision: str | None = None
    ) -> str:
        if not isinstance(data, dict):
            raise ValueError("Config-Root muss ein Mapping sein")
        with self._lock, self._file_lock(exclusive=True):
            self._load_unlocked()
            self._dirty_base_revision = None
            if expected_revision and expected_revision != self._revision:
                raise ConfigConflictError(
                    "Konfiguration wurde zwischenzeitlich geändert"
                )
            self._data = copy.deepcopy(data)
            self._save_unlocked()
            return self._revision

    def update(
        self,
        updater: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._file_lock(exclusive=True):
            self._load_unlocked()
            self._dirty_base_revision = None
            if expected_revision and expected_revision != self._revision:
                raise ConfigConflictError(
                    "Konfiguration wurde zwischenzeitlich geändert"
                )
            working = copy.deepcopy(self._data)
            result = updater(working)
            if result is not None:
                if not isinstance(result, dict):
                    raise ValueError("Config-Updater muss ein Mapping liefern")
                working = result
            self._data = working
            self._save_unlocked()
            return copy.deepcopy(self._data)

    def _atomic_write_bytes(self, destination: Path, raw: bytes) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, destination)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = yaml.safe_dump(self._data, allow_unicode=True, sort_keys=False).encode(
            "utf-8"
        )

        # Eine einzelne, restriktiv geschützte Vorversion ermöglicht ein schnelles
        # Rollback nach einem fehlerhaften UI-/CLI-Update, ohne Secrets in eine
        # wachsende Historie zu kopieren. Die geladene Altdatei war bereits YAML-validiert.
        if self.path.exists():
            previous = self.path.read_bytes()
            if previous != raw:
                self._atomic_write_bytes(
                    self.path.with_suffix(self.path.suffix + ".bak"), previous
                )

        self._atomic_write_bytes(self.path, raw)
        try:
            dir_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        self._revision = self._hash_bytes(raw)
        try:
            self._mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self._mtime_ns = 0

    def save(self) -> None:
        """Speichert lokale set()-Änderungen nur auf unveränderter Basisrevision."""
        with self._lock, self._file_lock(exclusive=True):
            disk_revision = "missing"
            if self.path.exists():
                disk_revision = self._hash_bytes(self.path.read_bytes())
            expected = self._dirty_base_revision or self._revision
            if disk_revision != expected:
                raise ConfigConflictError(
                    "Konfiguration wurde zwischen set() und save() geändert"
                )
            self._save_unlocked()
            self._dirty_base_revision = None

    def reload(self) -> None:
        with self._lock, self._file_lock(exclusive=False):
            self._load_unlocked()
            self._dirty_base_revision = None


_config: Optional[Config] = None
_config_lock = threading.Lock()


def get_config() -> Config:
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = Config(_CONFIG_PATH)
    return _config
