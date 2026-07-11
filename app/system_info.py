"""Leichtgewichtige Systemmetriken für das lokale Betriebs-Dashboard."""

from __future__ import annotations

import math
import os
import platform
import shutil
import socket
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in _read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        if len(parts) > 1 and parts[1].lower() == "kb":
            value *= 1024
        values[key] = value
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent_used": round((used * 100.0 / total), 1) if total else 0.0,
    }


def _cgroup_memory(root: Path = Path("/sys/fs/cgroup")) -> dict[str, Any] | None:
    """Liefert ein echtes Container-Limit für cgroup v2, sofern gesetzt."""
    maximum = _read_text(str(root / "memory.max"))
    current = _read_text(str(root / "memory.current"))
    if not maximum or maximum == "max" or not current:
        return None
    try:
        total = int(maximum)
        used = max(0, int(current))
    except ValueError:
        return None
    if total <= 0 or total >= (1 << 60):
        return None
    used = min(used, total)
    return {
        "total_bytes": total,
        "available_bytes": total - used,
        "used_bytes": used,
        "percent_used": round(used * 100.0 / total, 1),
        "source": "cgroup-v2",
    }


def _memory() -> dict[str, Any]:
    proc = _meminfo()
    proc["source"] = "proc"
    cgroup = _cgroup_memory()
    if cgroup and (
        not proc["total_bytes"] or cgroup["total_bytes"] < proc["total_bytes"]
    ):
        return cgroup
    return proc


def _cgroup_pids(root: Path = Path("/sys/fs/cgroup")) -> dict[str, Any]:
    current_raw = _read_text(str(root / "pids.current"))
    maximum_raw = _read_text(str(root / "pids.max"))
    try:
        current = max(0, int(current_raw))
    except (TypeError, ValueError):
        current = 0
    maximum: int | None
    if not maximum_raw or maximum_raw == "max":
        maximum = None
    else:
        try:
            parsed = int(maximum_raw)
            maximum = parsed if parsed > 0 else None
        except (TypeError, ValueError):
            maximum = None
    percent = round(current * 100.0 / maximum, 1) if maximum else None
    return {
        "current": current,
        "max": maximum,
        "percent_used": percent,
        "source": "cgroup-v2" if current_raw else "unknown",
    }


def _cpu_capacity(root: Path = Path("/sys/fs/cgroup")) -> tuple[int, float, str]:
    host_count = max(1, os.cpu_count() or 1)
    try:
        affinity_count = max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        affinity_count = host_count
    capacity = float(min(host_count, affinity_count))
    source = "affinity" if affinity_count < host_count else "system"
    raw = _read_text(str(root / "cpu.max")).split()
    if len(raw) == 2 and raw[0] != "max":
        try:
            quota = float(raw[0])
            period = float(raw[1])
            if quota > 0 and period > 0:
                quota_capacity = quota / period
                if quota_capacity < capacity:
                    capacity = quota_capacity
                    source = "cgroup-v2"
        except ValueError:
            pass
    capacity = max(0.01, capacity)
    return max(1, math.ceil(capacity)), capacity, source


def _uptime_seconds() -> float:
    raw = _read_text("/proc/uptime").split()
    try:
        return max(0.0, float(raw[0])) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


@lru_cache(maxsize=1)
def _virtualization() -> str:
    marker = _read_text("/run/systemd/container")
    if marker:
        return marker
    try:
        result = subprocess.run(
            ["systemd-detect-virt"],
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        )
        value = (result.stdout or "").strip()
        if value and value != "none":
            return value
    except (OSError, subprocess.TimeoutExpired):
        pass
    if Path("/.dockerenv").exists():
        return "docker"
    return "bare-metal/unknown"


def _primary_addresses() -> list[str]:
    found: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            address = str(item[4][0]).split("%", 1)[0]
            if address not in {"127.0.0.1", "::1"}:
                found.add(address)
    except OSError:
        pass
    return sorted(found)


def _disk(path: str) -> dict[str, Any]:
    target = Path(path)
    probe = target if target.exists() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
        used = usage.total - usage.free
        return {
            "path": path,
            "probe_path": str(probe),
            "total_bytes": usage.total,
            "used_bytes": used,
            "free_bytes": usage.free,
            "percent_used": round(used * 100.0 / usage.total, 1)
            if usage.total
            else 0.0,
        }
    except OSError as exc:
        return {"path": path, "error": str(exc)}


def system_snapshot(data_dir: str) -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0
    cpu_count, cpu_capacity, cpu_source = _cpu_capacity()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(aliased=True, terse=True),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "virtualization": _virtualization(),
        "addresses": _primary_addresses(),
        "uptime_seconds": _uptime_seconds(),
        "cpu": {
            "count": cpu_count,
            "capacity": round(cpu_capacity, 2),
            "source": cpu_source,
            "load_1": round(load1, 2),
            "load_5": round(load5, 2),
            "load_15": round(load15, 2),
            "load_percent": round(load1 * 100.0 / cpu_capacity, 1),
        },
        "memory": _memory(),
        "pids": _cgroup_pids(),
        "data_disk": _disk(data_dir),
        "generated_at": time.time(),
    }
