"""Storage-Übersicht pro Pair: lokaler freier Platz und optionale Remote-Größe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.rclone_sync import _is_remote
from ..jobs.scheduler import rclone_history_key
from ..rclone_args import rclone_subprocess_env
from ..security import require_csrf

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


_SIZE_CACHE_TTL_SECONDS = _bounded_env_int(
    "RCLONE_SIZE_CACHE_TTL_SECONDS", 300, 30, 3600
)
_SIZE_CACHE_MAX_ENTRIES = _bounded_env_int(
    "RCLONE_SIZE_CACHE_MAX_ENTRIES", 256, 16, 2048
)
_SIZE_MEASUREMENT_DEADLINE_SECONDS = _bounded_env_int(
    "RCLONE_SIZE_MEASUREMENT_DEADLINE_SECONDS", 60, 5, 70
)
_SIZE_MEASUREMENT_TIMEOUT_SECONDS = _bounded_env_int(
    "RCLONE_SIZE_MEASUREMENT_TIMEOUT_SECONDS", 45, 5, 60
)
_SIZE_MEASUREMENT_WORKERS = _bounded_env_int(
    "RCLONE_SIZE_MEASUREMENT_WORKERS", 8, 2, 16
)
_size_cache_lock = threading.Lock()
_size_cache: OrderedDict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = (
    OrderedDict()
)


def _disk_usage(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"path": path, "exists": False}
    try:
        usage = shutil.disk_usage(str(target))
        return {
            "path": path,
            "exists": True,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent_used": round(usage.used * 100.0 / usage.total, 1)
            if usage.total
            else 0,
        }
    except OSError as exc:
        return {"path": path, "exists": True, "error": str(exc)}


def _rclone_size(remote: str, timeout: float = 45) -> dict[str, Any]:
    if not remote:
        return {"path": remote, "error": "Pfad fehlt"}
    cache_dir = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")
    try:
        result = subprocess.run(
            ["rclone", "size", "--json", "--cache-dir", cache_dir, "--", remote],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            return {
                "path": remote,
                "count": data.get("count"),
                "bytes": data.get("bytes"),
            }
        return {
            "path": remote,
            "error": (result.stderr or result.stdout).strip()[:300],
        }
    except subprocess.TimeoutExpired:
        return {"path": remote, "error": "Timeout"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"path": remote, "error": str(exc)}


def _size_cache_key(
    pair: dict[str, Any], side: str, path: str
) -> tuple[str, str, str, str]:
    """Bindet Messwerte an Datenweg, Richtung, Seite und exakten Pfad."""
    return (
        rclone_history_key(pair),
        str(pair.get("direction") or "").lower(),
        side,
        path,
    )


def _is_successful_size(result: dict[str, Any]) -> bool:
    return (
        not result.get("error")
        and isinstance(result.get("count"), int)
        and isinstance(result.get("bytes"), int)
    )


def _decorate_measurement(
    result: dict[str, Any], *, measured_at: float | None, status: str
) -> dict[str, Any]:
    error = result.get("measurement_error") or result.get("error")
    state = {
        "fresh": "loaded",
        "cached": "loaded",
        "stale": "stale",
        "failed": "failed",
    }.get(status, "failed")
    return {
        **result,
        "measured_at": measured_at,
        "measurement_status": status,
        "measurement_state": state,
        "measurement_error": str(error) if error else None,
    }


def _measurement_metadata(result: dict[str, Any] | None, path: str) -> dict[str, Any]:
    """Kleine, stabile Zustandsprojektion für Clients ohne Größenmodell."""
    if not result:
        return {
            "path": path,
            "state": "loading",
            "measurement_error": None,
            "measured_at": None,
        }
    return {
        "path": result.get("path", path),
        "state": result.get("measurement_state", "failed"),
        "measurement_error": result.get("measurement_error"),
        "measured_at": result.get("measured_at"),
    }


def _cached_rclone_size(
    pair: dict[str, Any],
    side: str,
    path: str,
    *,
    force_refresh: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Liefert erfolgreiche Größenmessungen aus einem begrenzten TTL-Cache.

    Fehler werden nie als neue Messung gespeichert. Wenn eine erneute Messung
    scheitert, bleibt ein vorhandener alter Wert ausdrücklich als ``stale``
    erkennbar, statt wie ein frischer Nullwert auszusehen.
    """
    key = _size_cache_key(pair, side, path)
    now = time.time()
    with _size_cache_lock:
        cached = _size_cache.get(key)
        if cached and not force_refresh and now - cached[0] <= _SIZE_CACHE_TTL_SECONDS:
            _size_cache.move_to_end(key)
            return _decorate_measurement(
                dict(cached[1]), measured_at=cached[0], status="cached"
            )

    if deadline is None:
        result = _rclone_size(path)
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = {"path": path, "error": "Globale Messzeit überschritten"}
        else:
            result = _rclone_size(
                path,
                timeout=min(_SIZE_MEASUREMENT_TIMEOUT_SECONDS, max(0.1, remaining)),
            )
    measured_at = time.time()
    if _is_successful_size(result):
        clean = {
            "path": path,
            "count": result["count"],
            "bytes": result["bytes"],
        }
        with _size_cache_lock:
            _size_cache[key] = (measured_at, clean)
            _size_cache.move_to_end(key)
            while len(_size_cache) > _SIZE_CACHE_MAX_ENTRIES:
                _size_cache.popitem(last=False)
        return _decorate_measurement(clean, measured_at=measured_at, status="fresh")

    if cached:
        stale = _decorate_measurement(
            dict(cached[1]), measured_at=cached[0], status="stale"
        )
        stale["measurement_error"] = str(
            result.get("error") or "Messung fehlgeschlagen"
        )
        return stale
    return _decorate_measurement(
        {"path": path, "error": str(result.get("error") or "Messung fehlgeschlagen")},
        measured_at=None,
        status="failed",
    )


def _measurement_summary(
    output: list[dict[str, Any]], *, requested: bool
) -> dict[str, Any]:
    if not requested:
        return {
            "state": "loading",
            "total": len(output) * 2,
            "loaded": 0,
            "failed": 0,
            "stale": 0,
            "measurement_error": None,
            "measured_at": None,
        }

    results = [
        item.get(f"{side}_size") for item in output for side in ("source", "target")
    ]
    states = [
        str(result.get("measurement_state") or "failed")
        for result in results
        if isinstance(result, dict)
    ]
    loaded = states.count("loaded")
    failed = states.count("failed")
    stale = states.count("stale")
    total = len(results)
    if total == 0 or loaded == total:
        state = "loaded"
    elif failed == total:
        state = "failed"
    elif stale == total:
        state = "stale"
    else:
        state = "partial"
    measured_values = [
        float(result["measured_at"])
        for result in results
        if isinstance(result, dict)
        and isinstance(result.get("measured_at"), (int, float))
    ]
    return {
        "state": state,
        "total": total,
        "loaded": loaded,
        "failed": failed,
        "stale": stale,
        "measurement_error": (
            f"{loaded + stale} von {total} Messungen nutzbar"
            if failed or stale
            else None
        ),
        "measured_at": max(measured_values, default=None),
    }


def _resolve_endpoints(pair: dict[str, Any]) -> tuple[str, str]:
    """Löst Quelle/Ziel eines Pairs anhand der Richtung auf (wie die GUI-Anzeige).

    pull: Remote -> lokal (Quelle=Remote, Ziel=lokal). push/bisync: lokal -> Remote.
    """
    local = str(pair.get("local") or "")
    remote = str(pair.get("remote") or "")
    if str(pair.get("direction") or "").lower() == "pull":
        return remote, local
    return local, remote


def _last_success_by_identity(
    pairs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Mappt echte Sync-Erfolge auf die aktuell konfigurierten Pair-Namen.

    Die stabile rclone-Identität überlebt Umbenennungen und verhindert, dass
    ein Restore-Drill oder ein inzwischen anders belegter Anzeigename als letzter
    erfolgreicher Sync erscheint.
    """
    identities = {
        rclone_history_key(pair): str(pair.get("name") or "").strip()
        for pair in pairs
        if isinstance(pair, dict) and str(pair.get("name") or "").strip()
    }
    histories = get_db().pair_last_history(identities) if identities else {}
    found: dict[str, dict[str, Any]] = {}
    for history_key in identities:
        result = (histories.get(history_key) or {}).get("last_success")
        if not result:
            continue
        pair = result.get("pair") or {}
        entry: dict[str, Any] = {"last_sync": result.get("ended_at")}
        transferred = pair.get("transferred")
        if transferred not in (None, ""):
            # Historische Datensätze enthielten teils Byte-Zahlen, neuere
            # rclone-Auswertungen bereits formatierte Texte. Der API-Vertrag
            # bleibt für beide Fälle stabil und liefert hier immer Text.
            entry["last_transferred"] = str(transferred)
        found[history_key] = entry
    return found


@router.get("/overview")
def overview(
    include_remote: bool = False, refresh_sizes: bool = False
) -> dict[str, Any]:
    pairs = [
        pair
        for pair in (get_config().get("backup", "pairs", default=[]) or [])
        if isinstance(pair, dict)
    ]
    last_success = _last_success_by_identity(pairs)
    output: list[dict[str, Any]] = []
    for pair in pairs:
        local = str(pair.get("local") or "")
        name = str(pair.get("name") or "")
        source, target = _resolve_endpoints(pair)
        info: dict[str, Any] = {
            "name": name,
            "local": local,
            "remote": pair.get("remote"),
            "direction": pair.get("direction", ""),
            "source": source,
            "target": target,
            "schedule": pair.get("schedule", ""),
            "local_disk": _disk_usage(local)
            if local and not _is_remote(local)
            else None,
            **last_success.get(rclone_history_key(pair), {}),
        }
        info["source_measurement"] = _measurement_metadata(None, source)
        info["target_measurement"] = _measurement_metadata(None, target)
        output.append(info)

    # Größen für Quelle UND Ziel jedes Pairs sind teuer (rclone size traversiert
    # beide Endpunkte). Daher nur auf ausdrückliche Anforderung und parallelisiert.
    if include_remote and output:
        deadline = time.monotonic() + _SIZE_MEASUREMENT_DEADLINE_SECONDS
        tasks: list[tuple[int, str, dict[str, Any]]] = []
        for index, (item, pair) in enumerate(zip(output, pairs, strict=True)):
            tasks.append((index, "source", pair))
            tasks.append((index, "target", pair))
        workers = min(_SIZE_MEASUREMENT_WORKERS, max(1, len(tasks)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pair-size"
        ) as pool:
            futures = {
                pool.submit(
                    _cached_rclone_size,
                    pair,
                    side,
                    str(output[index].get(side) or ""),
                    force_refresh=refresh_sizes,
                    deadline=deadline,
                ): (
                    index,
                    side,
                )
                for index, side, pair in tasks
            }
            for future in as_completed(futures):
                index, side = futures[future]
                key = f"{side}_size"
                try:
                    output[index][key] = future.result()
                except Exception as exc:
                    output[index][key] = _decorate_measurement(
                        {
                            "path": output[index].get(side),
                            "error": str(exc),
                        },
                        measured_at=None,
                        status="failed",
                    )
                output[index][f"{side}_measurement"] = _measurement_metadata(
                    output[index][key], str(output[index].get(side) or "")
                )
    return {
        "pairs": output,
        "measurement": _measurement_summary(output, requested=include_remote),
    }
