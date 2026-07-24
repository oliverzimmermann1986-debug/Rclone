"""Bounded, incremental log-tail reads for frequently polled job progress."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _CacheEntry:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    data: bytes


_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()
_CACHE_MAX_BYTES = 16 * 1024 * 1024
_CACHE_MAX_ENTRIES = 128
_CACHE_BYTES = 0


def _stat_ns(stat_result: os.stat_result, name: str, seconds_name: str) -> int:
    value = getattr(stat_result, name, None)
    if value is not None:
        return int(value)
    return int(float(getattr(stat_result, seconds_name)) * 1_000_000_000)


def _entry(stat_result: os.stat_result, data: bytes) -> _CacheEntry:
    return _CacheEntry(
        device=int(getattr(stat_result, "st_dev", 0) or 0),
        inode=int(getattr(stat_result, "st_ino", 0) or 0),
        size=int(stat_result.st_size),
        mtime_ns=_stat_ns(stat_result, "st_mtime_ns", "st_mtime"),
        ctime_ns=_stat_ns(stat_result, "st_ctime_ns", "st_ctime"),
        data=data,
    )


def _unchanged(cached: _CacheEntry, stat_result: os.stat_result) -> bool:
    current = _entry(stat_result, b"")
    return (
        cached.device == current.device
        and cached.inode == current.inode
        and cached.size == current.size
        and cached.mtime_ns == current.mtime_ns
        and cached.ctime_ns == current.ctime_ns
    )


def _same_file(cached: _CacheEntry, stat_result: os.stat_result) -> bool:
    device = int(getattr(stat_result, "st_dev", 0) or 0)
    inode = int(getattr(stat_result, "st_ino", 0) or 0)
    # Ohne brauchbare File-ID wird sicherheitshalber neu gelesen.
    return bool(cached.inode and inode) and (
        cached.device == device and cached.inode == inode
    )


def _cache_put(cache_key: str, stat_result: os.stat_result, data: bytes) -> None:
    global _CACHE_BYTES
    budget = max(0, int(_CACHE_MAX_BYTES))
    cached_data = data[-budget:] if budget else b""
    with _CACHE_LOCK:
        previous = _CACHE.pop(cache_key, None)
        if previous:
            _CACHE_BYTES -= len(previous.data)
        if not cached_data:
            _CACHE_BYTES = max(0, _CACHE_BYTES)
            return
        while _CACHE and (
            _CACHE_BYTES + len(cached_data) > budget
            or len(_CACHE) >= _CACHE_MAX_ENTRIES
        ):
            _, evicted = _CACHE.popitem(last=False)
            _CACHE_BYTES -= len(evicted.data)
        if len(cached_data) <= budget:
            value = _entry(stat_result, cached_data)
            _CACHE[cache_key] = value
            _CACHE_BYTES += len(value.data)


def _clear_cache() -> None:
    global _CACHE_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0


def _cache_drop(cache_key: str) -> None:
    global _CACHE_BYTES
    with _CACHE_LOCK:
        previous = _CACHE.pop(cache_key, None)
        if previous:
            _CACHE_BYTES = max(0, _CACHE_BYTES - len(previous.data))


def read_tail(path: Path, max_bytes: int = 1024 * 1024) -> str:
    try:
        cache_key = str(path)
        limit = max(1024, int(max_bytes))
        stat_result = path.stat()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if (
                cached
                and _unchanged(cached, stat_result)
                and len(cached.data) >= min(limit, cached.size)
            ):
                _CACHE.move_to_end(cache_key)
                return cached.data[-limit:].decode("utf-8", errors="ignore")

        with path.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            cached_capacity = max(limit, len(cached.data) if cached else 0)
            can_extend = bool(
                cached
                and _same_file(cached, opened_stat)
                and opened_stat.st_size > cached.size
                and len(cached.data) >= min(limit, cached.size)
                and opened_stat.st_size - cached.size <= cached_capacity
            )
            if can_extend and cached is not None:
                handle.seek(cached.size, os.SEEK_SET)
                cache_data = (cached.data + handle.read(cached_capacity))[
                    -cached_capacity:
                ]
                raw = cache_data[-limit:]
            else:
                handle.seek(max(0, opened_stat.st_size - limit), os.SEEK_SET)
                raw = handle.read(limit)
                cache_data = raw
            final_stat = os.fstat(handle.fileno())
            reached_eof = handle.tell() == final_stat.st_size

        text = raw.decode("utf-8", errors="ignore")
        if reached_eof:
            _cache_put(cache_key, final_stat, cache_data)
        else:
            _cache_drop(cache_key)
        return text
    except (OSError, TypeError, ValueError):
        return ""
