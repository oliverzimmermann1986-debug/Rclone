"""Bounded, incremental log-tail reads for frequently polled job progress."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
_CACHE_LIMIT = 64


def read_tail(path: Path, max_bytes: int = 1024 * 1024) -> str:
    try:
        stat_result = path.stat()
        cache_key = str(path)
        limit = max(1024, max_bytes)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and cached[0] == stat_result.st_size and cached[1] == limit:
                _CACHE.move_to_end(cache_key)
                return cached[2]

        with path.open("rb") as handle:
            if cached and cached[1] == limit and stat_result.st_size > cached[0]:
                handle.seek(cached[0])
                raw = cached[2].encode("utf-8") + handle.read()
                raw = raw[-limit:]
            else:
                handle.seek(max(0, stat_result.st_size - limit), os.SEEK_SET)
                raw = handle.read()

        text = raw.decode("utf-8", errors="ignore")
        with _CACHE_LOCK:
            _CACHE[cache_key] = (stat_result.st_size, limit, text)
            _CACHE.move_to_end(cache_key)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
        return text
    except OSError:
        return ""
