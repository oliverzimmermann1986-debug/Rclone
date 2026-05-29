"""Minimaler Config-Store: lädt config.yaml, bietet get/set + save.
Kompatibel mit dem alten scrapper-Konfig-Layout."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

_CONFIG_PATH = Path(os.getenv("RCLONE_SYNC_CONFIG", "/opt/rclone-sync/data/config.yaml"))
_lock = threading.Lock()


class Config:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}

    def get(self, *keys, default=None):
        cur: Any = self._data
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k, default)
            if cur is default:
                return default
        return cur

    def set(self, *keys_and_value) -> None:
        if len(keys_and_value) < 2:
            raise ValueError("set: mindestens key + value")
        *keys, value = keys_and_value
        cur = self._data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
            if not isinstance(cur, dict):
                raise ValueError(f"set: '{k}' ist kein dict")
        cur[keys[-1]] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, sort_keys=False)
        tmp.replace(self.path)

    def reload(self) -> None:
        with _lock:
            self._load()


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                _config = Config(_CONFIG_PATH)
    return _config
