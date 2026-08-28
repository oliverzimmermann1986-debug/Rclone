"""Storage-Übersicht pro Pair: lokaler freier Platz und optionale Remote-Größe."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.rclone_sync import _filter_args, _is_remote
from ..jobs.restore_test import PAIR_PREFIX as RESTORE_PAIR_PREFIX
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
_size_cache: OrderedDict[
    tuple[str, str, str, str, tuple[str, ...]], tuple[float, dict[str, Any]]
] = OrderedDict()
_size_inflight: dict[
    tuple[str, str, str, str, tuple[str, ...]], Future[dict[str, Any]]
] = {}

_COMPOSITION_CACHE_TTL_SECONDS = _bounded_env_int(
    "RCLONE_COMPOSITION_CACHE_TTL_SECONDS", 900, 60, 7200
)
_COMPOSITION_CACHE_MAX_ENTRIES = _bounded_env_int(
    "RCLONE_COMPOSITION_CACHE_MAX_ENTRIES", 128, 8, 1024
)
_COMPOSITION_TIMEOUT_SECONDS = _bounded_env_int(
    "RCLONE_COMPOSITION_TIMEOUT_SECONDS", 60, 5, 180
)
_COMPOSITION_MAX_FILES = _bounded_env_int(
    "RCLONE_COMPOSITION_MAX_FILES", 100_000, 1_000, 1_000_000
)
_composition_cache_lock = threading.Lock()
_composition_cache: OrderedDict[
    tuple[str, str, str, str, tuple[str, ...]], tuple[float, dict[str, Any]]
] = OrderedDict()
_composition_inflight: dict[
    tuple[str, str, str, str, tuple[str, ...]], Future[dict[str, Any]]
] = {}

_FILE_CATEGORIES: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "images",
        "Bilder",
        frozenset(
            {
                "jpg",
                "jpeg",
                "png",
                "heic",
                "heif",
                "gif",
                "webp",
                "tif",
                "tiff",
                "bmp",
                "raw",
                "dng",
            }
        ),
    ),
    (
        "videos",
        "Videos",
        frozenset({"mp4", "mov", "mkv", "avi", "m4v", "webm", "mpeg", "mpg"}),
    ),
    (
        "documents",
        "Dokumente",
        frozenset(
            {
                "pdf",
                "doc",
                "docx",
                "xls",
                "xlsx",
                "ppt",
                "pptx",
                "txt",
                "md",
                "rtf",
                "odt",
                "ods",
                "csv",
            }
        ),
    ),
    (
        "audio",
        "Audio",
        frozenset({"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma"}),
    ),
    ("archives", "Archive", frozenset({"zip", "7z", "rar", "tar", "gz", "bz2", "xz"})),
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


def _rclone_size(
    remote: str, timeout: float = 45, filter_args: Sequence[str] = ()
) -> dict[str, Any]:
    if not remote:
        return {"path": remote, "error": "Pfad fehlt"}
    cache_dir = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")
    try:
        result = subprocess.run(
            [
                "rclone",
                "size",
                "--json",
                "--cache-dir",
                cache_dir,
                *(str(value) for value in filter_args),
                "--",
                remote,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        data: dict[str, Any] | None = None
        try:
            parsed = json.loads(result.stdout) if result.stdout.strip() else None
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            if result.returncode == 0:
                raise

        if result.returncode == 0:
            data = data or {}
            return {
                "path": remote,
                "count": data.get("count"),
                "bytes": data.get("bytes"),
            }
        error = (result.stderr or result.stdout).strip()[:300]
        partial = {
            "path": remote,
            "error": error,
        }
        if (
            data is not None
            and isinstance(data.get("count"), int)
            and isinstance(data.get("bytes"), int)
        ):
            partial.update(count=data["count"], bytes=data["bytes"])
        return partial
    except subprocess.TimeoutExpired:
        return {"path": remote, "error": "Timeout"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"path": remote, "error": str(exc)}


def _size_cache_key(
    pair: dict[str, Any], side: str, path: str, filter_args: Sequence[str] = ()
) -> tuple[str, str, str, str, tuple[str, ...]]:
    """Bindet Messwerte an Datenweg, Richtung, Seite und exakten Pfad."""
    return (
        rclone_history_key(pair),
        str(pair.get("direction") or "").lower(),
        side,
        path,
        tuple(str(value) for value in filter_args),
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
        "partial": "failed",
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
    filter_args: Sequence[str] = (),
    force_refresh: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Liefert erfolgreiche Größenmessungen aus einem begrenzten TTL-Cache.

    Fehler werden nie als neue Messung gespeichert. Wenn eine erneute Messung
    scheitert, bleibt ein vorhandener alter Wert ausdrücklich als ``stale``
    erkennbar, statt wie ein frischer Nullwert auszusehen.
    """
    normalized_filter_args = tuple(str(value) for value in filter_args)
    key = _size_cache_key(pair, side, path, normalized_filter_args)
    now = time.time()
    with _size_cache_lock:
        cached = _size_cache.get(key)
        if cached and not force_refresh and now - cached[0] <= _SIZE_CACHE_TTL_SECONDS:
            _size_cache.move_to_end(key)
            return _decorate_measurement(
                dict(cached[1]), measured_at=cached[0], status="cached"
            )
        flight = _size_inflight.get(key)
        leader = flight is None
        if flight is None:
            flight = Future()
            _size_inflight[key] = flight

    # Wait outside the cache lock: other identities must remain free to start
    # and complete while this measurement is running.
    if not leader:
        return dict(flight.result())

    try:
        if deadline is None:
            result = (
                _rclone_size(path, filter_args=normalized_filter_args)
                if normalized_filter_args
                else _rclone_size(path)
            )
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = {"path": path, "error": "Globale Messzeit überschritten"}
            else:
                measure_kwargs: dict[str, Any] = {
                    "timeout": min(
                        _SIZE_MEASUREMENT_TIMEOUT_SECONDS, max(0.1, remaining)
                    )
                }
                if normalized_filter_args:
                    measure_kwargs["filter_args"] = normalized_filter_args
                result = _rclone_size(path, **measure_kwargs)
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
            decorated = _decorate_measurement(
                clean, measured_at=measured_at, status="fresh"
            )
        elif isinstance(result.get("count"), int) and isinstance(
            result.get("bytes"), int
        ):
            partial = {
                "path": path,
                "count": result["count"],
                "bytes": result["bytes"],
                "measurement_error": str(
                    result.get("error") or "Messung nur teilweise erfolgreich"
                ),
            }
            decorated = _decorate_measurement(
                partial, measured_at=measured_at, status="partial"
            )
        elif cached:
            decorated = _decorate_measurement(
                dict(cached[1]), measured_at=cached[0], status="stale"
            )
            decorated["measurement_error"] = str(
                result.get("error") or "Messung fehlgeschlagen"
            )
        else:
            decorated = _decorate_measurement(
                {
                    "path": path,
                    "error": str(result.get("error") or "Messung fehlgeschlagen"),
                },
                measured_at=None,
                status="failed",
            )
        flight.set_result(dict(decorated))
        return decorated
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with _size_cache_lock:
            if _size_inflight.get(key) is flight:
                del _size_inflight[key]


def _cached_pair_rclone_size(
    cfg,
    pair: dict[str, Any],
    side: str,
    path: str,
    *,
    force_refresh: bool = False,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Misst einen Pair-Endpunkt mit denselben Filtern wie der echte Sync."""
    return _cached_rclone_size(
        pair,
        side,
        path,
        filter_args=tuple(_filter_args(cfg, pair, "size")),
        force_refresh=force_refresh,
        deadline=deadline,
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


def _find_pair(cfg, identity: str) -> dict[str, Any]:
    needle = str(identity or "").strip()
    for pair in cfg.get("backup", "pairs", default=[]) or []:
        if not isinstance(pair, dict):
            continue
        if needle in {str(pair.get("id") or ""), str(pair.get("name") or "")}:
            return pair
    raise HTTPException(status_code=404, detail="Datenweg nicht gefunden")


def _file_kind(path: str) -> tuple[str, str, str]:
    suffix = Path(path).suffix.lower().lstrip(".")
    if not suffix:
        return "without_extension", "Ohne Endung", "Ohne Endung"
    for key, label, extensions in _FILE_CATEGORIES:
        if suffix in extensions:
            return key, label, suffix.upper()
    return "other", "Sonstige", suffix.upper()


def _aggregate_buckets(
    values: dict[str, dict[str, Any]], *, limit: int | None = None
) -> list[dict[str, Any]]:
    rows = sorted(
        values.values(),
        key=lambda item: (int(item["bytes"]), int(item["count"])),
        reverse=True,
    )
    if limit is None or len(rows) <= limit:
        return rows
    visible = rows[:limit]
    remaining = rows[limit:]
    visible.append(
        {
            "key": "remaining",
            "label": "Weitere",
            "count": sum(int(item["count"]) for item in remaining),
            "bytes": sum(int(item["bytes"]) for item in remaining),
        }
    )
    return visible


def _rclone_composition(
    path: str,
    *,
    filter_args: Sequence[str] = (),
    timeout: int = _COMPOSITION_TIMEOUT_SECONDS,
    max_files: int = _COMPOSITION_MAX_FILES,
) -> dict[str, Any]:
    """Aggregiert Typen lokal im Serverprozess, ohne Dateinamen offenzulegen."""
    if not path:
        return {"path": path, "error": "Pfad fehlt"}
    cache_dir = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")
    command = [
        "rclone",
        "lsf",
        "--recursive",
        "--files-only",
        "--csv",
        "--format",
        "sp",
        "--cache-dir",
        cache_dir,
        *(str(value) for value in filter_args),
        "--",
        path,
    ]
    timed_out = threading.Event()
    process: subprocess.Popen[str] | None = None

    def stop_for_timeout() -> None:
        timed_out.set()
        if process is not None and process.poll() is None:
            process.kill()

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            # Error output can contain private object names and grow very large
            # on permission failures. The API intentionally reports only a
            # generic status and never returns that stream.
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        assert process.stdout is not None
        timer = threading.Timer(timeout, stop_for_timeout)
        timer.daemon = True
        timer.start()
        categories: dict[str, dict[str, Any]] = {}
        extensions: dict[str, dict[str, Any]] = {}
        total_count = 0
        total_bytes = 0
        truncated = False
        try:
            for row in csv.reader(process.stdout):
                if len(row) < 2:
                    continue
                try:
                    size = max(0, int(row[0]))
                except ValueError:
                    continue
                file_path = ",".join(row[1:])
                category_key, category_label, extension_label = _file_kind(file_path)
                category = categories.setdefault(
                    category_key,
                    {
                        "key": category_key,
                        "label": category_label,
                        "count": 0,
                        "bytes": 0,
                    },
                )
                extension_key = extension_label.casefold().replace(" ", "_")
                extension = extensions.setdefault(
                    extension_key,
                    {
                        "key": extension_key,
                        "label": extension_label,
                        "count": 0,
                        "bytes": 0,
                    },
                )
                for bucket in (category, extension):
                    bucket["count"] += 1
                    bucket["bytes"] += size
                total_count += 1
                total_bytes += size
                if total_count >= max_files:
                    truncated = True
                    process.terminate()
                    break
        finally:
            timer.cancel()

        try:
            return_code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=3)
        result = {
            "path": path,
            "count": total_count,
            "bytes": total_bytes,
            "truncated": truncated,
            "categories": _aggregate_buckets(categories),
            "extensions": _aggregate_buckets(extensions, limit=10),
        }
        if timed_out.is_set():
            result["error"] = f"Timeout nach {timeout}s"
        elif return_code != 0 and not truncated:
            result["error"] = (
                f"Dateityp-Analyse fehlgeschlagen (rclone exit={return_code})"
            )
        return result
    except FileNotFoundError:
        return {"path": path, "error": "rclone Binary nicht gefunden"}
    except (OSError, ValueError, csv.Error) as exc:
        return {"path": path, "error": str(exc)}
    finally:
        if process is not None and process.poll() is None:
            process.kill()


def _cached_composition(
    pair: dict[str, Any],
    side: str,
    path: str,
    *,
    filter_args: Sequence[str] = (),
    force_refresh: bool = False,
) -> dict[str, Any]:
    normalized_filters = tuple(str(value) for value in filter_args)
    key = _size_cache_key(pair, side, path, normalized_filters)
    now = time.time()
    with _composition_cache_lock:
        cached = _composition_cache.get(key)
        if (
            cached
            and not force_refresh
            and now - cached[0] <= _COMPOSITION_CACHE_TTL_SECONDS
        ):
            _composition_cache.move_to_end(key)
            return {**cached[1], "status": "cached", "measured_at": cached[0]}
        flight = _composition_inflight.get(key)
        leader = flight is None
        if flight is None:
            flight = Future()
            _composition_inflight[key] = flight
    if not leader:
        return dict(flight.result())

    try:
        result = _rclone_composition(path, filter_args=normalized_filters)
        measured_at = time.time()
        if not result.get("error"):
            clean = dict(result)
            with _composition_cache_lock:
                _composition_cache[key] = (measured_at, clean)
                _composition_cache.move_to_end(key)
                while len(_composition_cache) > _COMPOSITION_CACHE_MAX_ENTRIES:
                    _composition_cache.popitem(last=False)
            decorated = {**clean, "status": "fresh", "measured_at": measured_at}
        elif int(result.get("count") or 0) > 0:
            # Wie bei der Größenmessung bleiben verwertbare Teilresultate
            # sichtbar, werden aber nie als vollständiger Cache-Stand abgelegt.
            decorated = {**result, "status": "partial", "measured_at": measured_at}
        elif cached:
            decorated = {
                **cached[1],
                "status": "stale",
                "measured_at": cached[0],
                "error": str(result.get("error") or "Messung fehlgeschlagen"),
            }
        else:
            decorated = {**result, "status": "failed", "measured_at": None}
        flight.set_result(dict(decorated))
        return decorated
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with _composition_cache_lock:
            if _composition_inflight.get(key) is flight:
                del _composition_inflight[key]


def _history_evidence_by_identity(
    pairs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Mappt echte Sync-Erfolge und Restore-Nachweise auf Datenwege.

    Die stabile rclone-Identität überlebt Umbenennungen und verhindert, dass
    ein Restore-Drill oder ein inzwischen anders belegter Anzeigename als letzter
    erfolgreicher Sync erscheint. Restore-Nachweise bleiben über ihren eigenen
    getypten Schlüssel ausdrücklich von Sync-Erfolgen getrennt.
    """
    sync_identities = {
        rclone_history_key(pair): str(pair.get("name") or "").strip()
        for pair in pairs
        if isinstance(pair, dict) and str(pair.get("name") or "").strip()
    }
    restore_identities = {
        f"{RESTORE_PAIR_PREFIX}{str(pair.get('name') or '').strip()}": str(
            pair.get("name") or ""
        ).strip()
        for pair in pairs
        if isinstance(pair, dict) and str(pair.get("name") or "").strip()
    }
    identities = {**sync_identities, **restore_identities}
    histories = get_db().pair_last_history(identities) if identities else {}

    sync_found: dict[str, dict[str, Any]] = {}
    for history_key in sync_identities:
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
        sync_found[history_key] = entry

    restore_found: dict[str, dict[str, Any]] = {}
    for history_key, name in restore_identities.items():
        history = histories.get(history_key) or {}
        last_result = history.get("last_result")
        last_success = history.get("last_success")
        if not last_result and not last_success:
            restore_found[name] = {
                "state": "never",
                "last_attempt_at": None,
                "last_success_at": None,
                "job_id": None,
                "verified_files": None,
                "sample_size": None,
                "checksum_verified": False,
                "error": None,
            }
            continue

        result_pair = (last_result or {}).get("pair") or {}
        proof_pair = (last_success or {}).get("pair") or {}
        restore_item = {
            "state": "passed" if last_result and last_result.get("ok") else "failed",
            "last_attempt_at": (last_result or {}).get("ended_at"),
            "last_success_at": (last_success or {}).get("ended_at"),
            "job_id": (last_result or {}).get("job_id"),
            "verified_files": proof_pair.get("verified"),
            "sample_size": proof_pair.get("sample_size"),
            # Ein erfolgreicher Restore-Drill wird erst nach rclone check
            # --checksum gespeichert; der Erfolg selbst ist der Nachweis.
            "checksum_verified": bool(last_success),
            "error": result_pair.get("error")
            if last_result and not last_result.get("ok")
            else None,
        }
        if (last_result or {}).get("ended_at") and (last_result or {}).get(
            "started_at"
        ):
            restore_item["duration_sec"] = max(
                0.0,
                float((last_result or {}).get("ended_at") or 0)
                - float((last_result or {}).get("started_at") or 0),
            )
        restore_found[name] = restore_item
    return sync_found, restore_found


@router.get("/overview")
def overview(
    include_remote: bool = False, refresh_sizes: bool = False
) -> dict[str, Any]:
    cfg = get_config()
    pairs = [
        pair
        for pair in (cfg.get("backup", "pairs", default=[]) or [])
        if isinstance(pair, dict)
    ]
    last_success, restore_evidence = _history_evidence_by_identity(pairs)
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
            "restore_evidence": restore_evidence.get(name),
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
                    _cached_pair_rclone_size,
                    cfg,
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


@router.get("/composition")
def composition(
    pair: str = Query(min_length=1, max_length=200),
    side: str = Query(pattern="^(source|target)$"),
    refresh: bool = False,
) -> dict[str, Any]:
    """Dateityp-Verteilung eines Datenweg-Endpunkts für die Detailansicht."""
    cfg = get_config()
    selected = _find_pair(cfg, pair)
    source, target = _resolve_endpoints(selected)
    path = source if side == "source" else target
    result = _cached_composition(
        selected,
        side,
        path,
        filter_args=tuple(_filter_args(cfg, selected, "size")),
        force_refresh=refresh,
    )
    return {
        "pair": str(selected.get("name") or pair),
        "side": side,
        **result,
    }
