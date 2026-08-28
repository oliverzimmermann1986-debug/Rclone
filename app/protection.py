"""Evidence-based protection scoring and destructive-change quarantine."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .db import Database
from .jobs.scheduler import rclone_history_key
from .rclone_args import rclone_subprocess_env

_ANOMALY_STATE_KEY = "anomaly_guard:v1"
_MAX_QUARANTINES = 256


POLICY_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "family_photos",
        "name": "Familienfotos",
        "description": "Tägliche Kopie ohne automatische Löschungen und mit monatlichem Restore-Nachweis.",
        "pair": {"mode": "copy", "allow_delete": False, "min_local_files": 1},
        "job": {"schedule": "0 3 * * *", "retry_minutes": 60},
        "restore": {"schedule": "0 5 1 * *", "sample_files": 20},
    },
    {
        "id": "documents",
        "name": "Dokumente",
        "description": "Tägliche Sicherung mit Versionsablage und eng begrenzten Löschungen.",
        "pair": {
            "mode": "sync",
            "allow_delete": True,
            "max_delete": 25,
            "backup_dir": ".rclone-versions/{date}",
            "min_local_files": 1,
        },
        "job": {"schedule": "30 2 * * *", "retry_minutes": 30},
        "restore": {"schedule": "0 5 * * 0", "sample_files": 30},
    },
    {
        "id": "archive",
        "name": "Archiv",
        "description": "Wöchentliche, bandbreitenschonende Kopie für große unveränderliche Bestände.",
        "pair": {"mode": "copy", "allow_delete": False, "min_local_files": 1},
        "job": {"schedule": "0 1 * * 0", "retry_minutes": 180},
        "restore": {"schedule": "0 6 1 * *", "sample_files": 10},
    },
    {
        "id": "critical",
        "name": "Kritische Daten",
        "description": "Engmaschige Sicherung, Schutzdatei, niedrige Löschgrenze und wöchentliche Notfallübung.",
        "pair": {
            "mode": "sync",
            "allow_delete": True,
            "max_delete": 10,
            "backup_dir": ".rclone-versions/{date}",
            "min_local_files": 1,
            "require_mountpoint": True,
            "sentinel_file": ".rclone-source",
        },
        "job": {"schedule": "0 */6 * * *", "retry_minutes": 15},
        "restore": {"schedule": "0 5 * * 0", "sample_files": 50},
    },
)


def is_destructive(pair: Mapping[str, Any]) -> bool:
    direction = str(pair.get("direction") or "").casefold()
    mode = str(pair.get("mode") or "").casefold()
    return direction == "bisync" or mode == "sync"


def guard_settings(
    backup: Mapping[str, Any], pair: Mapping[str, Any]
) -> dict[str, Any]:
    raw = backup.get("anomaly_guard")
    settings = raw if isinstance(raw, Mapping) else {}
    pair_raw = pair.get("anomaly_guard")
    pair_settings = pair_raw if isinstance(pair_raw, Mapping) else {}

    def value(name: str, default: Any) -> Any:
        return pair_settings.get(name, settings.get(name, default))

    return {
        "enabled": bool(value("enabled", True)),
        "file_drop_percent": float(value("file_drop_percent", 35)),
        "size_drop_percent": float(value("size_drop_percent", 35)),
        "min_baseline_files": int(value("min_baseline_files", 100)),
        "measurement_timeout_seconds": int(value("measurement_timeout_seconds", 180)),
    }


def _state_key(pair: Mapping[str, Any]) -> str:
    identity = rclone_history_key(dict(pair))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _load_state(database: Database) -> dict[str, Any]:
    raw = database.runtime_get(_ANOMALY_STATE_KEY, {})
    if not isinstance(raw, dict):
        return {"baselines": {}, "quarantines": {}}
    baselines = raw.get("baselines")
    quarantines = raw.get("quarantines")
    return {
        "baselines": baselines if isinstance(baselines, dict) else {},
        "quarantines": quarantines if isinstance(quarantines, dict) else {},
    }


def _save_state(database: Database, state: Mapping[str, Any]) -> None:
    baselines = dict(state.get("baselines") or {})
    quarantines = dict(state.get("quarantines") or {})
    if len(baselines) > _MAX_QUARANTINES:
        ordered = sorted(
            baselines.items(),
            key=lambda item: float((item[1] or {}).get("measured_at") or 0),
            reverse=True,
        )[:_MAX_QUARANTINES]
        baselines = dict(ordered)
    if len(quarantines) > _MAX_QUARANTINES:
        ordered = sorted(
            quarantines.items(),
            key=lambda item: float((item[1] or {}).get("detected_at") or 0),
            reverse=True,
        )[:_MAX_QUARANTINES]
        quarantines = dict(ordered)
    database.runtime_set(
        _ANOMALY_STATE_KEY,
        {"baselines": baselines, "quarantines": quarantines},
    )


def measure_path(
    path: str,
    *,
    filter_args: Sequence[str] = (),
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Measure an endpoint without interpreting a failed measurement as zero."""

    try:
        result = subprocess.run(
            [
                "rclone",
                "size",
                "--json",
                *(str(value) for value in filter_args),
                "--",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=max(10, min(int(timeout_seconds), 900)),
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Größenmessung hat das Zeitlimit überschritten"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode != 0:
        return {
            "ok": False,
            "error": (
                result.stderr or result.stdout or f"exit {result.returncode}"
            ).strip()[:500],
        }
    try:
        payload = json.loads(result.stdout or "{}")
        count = int(payload["count"])
        size = int(payload["bytes"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Ungültige Größenantwort: {exc}"}
    if count < 0 or size < 0:
        return {"ok": False, "error": "Größenmessung enthält negative Werte"}
    return {"ok": True, "count": count, "bytes": size, "measured_at": time.time()}


def evaluate_anomaly(
    baseline: Mapping[str, Any] | None,
    measurement: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    if not measurement.get("ok"):
        return {"blocked": False, "measured": False, "error": measurement.get("error")}
    if not baseline:
        return {"blocked": False, "measured": True, "reason": "baseline_created"}
    old_count = max(0, int(baseline.get("count") or 0))
    old_bytes = max(0, int(baseline.get("bytes") or 0))
    new_count = max(0, int(measurement.get("count") or 0))
    new_bytes = max(0, int(measurement.get("bytes") or 0))
    minimum = max(1, int(settings.get("min_baseline_files") or 100))
    if old_count < minimum:
        return {"blocked": False, "measured": True, "reason": "baseline_too_small"}
    file_drop = 100.0 * max(0, old_count - new_count) / max(1, old_count)
    size_drop = 100.0 * max(0, old_bytes - new_bytes) / max(1, old_bytes)
    file_limit = float(settings.get("file_drop_percent") or 35)
    size_limit = float(settings.get("size_drop_percent") or 35)
    blocked = file_drop >= file_limit or size_drop >= size_limit
    return {
        "blocked": blocked,
        "measured": True,
        "previous_count": old_count,
        "current_count": new_count,
        "previous_bytes": old_bytes,
        "current_bytes": new_bytes,
        "file_drop_percent": round(file_drop, 2),
        "size_drop_percent": round(size_drop, 2),
        "file_drop_limit": file_limit,
        "size_drop_limit": size_limit,
        "reason": "unexpected_source_drop" if blocked else "within_limits",
    }


def preflight_anomaly_guard(
    database: Database,
    *,
    pair: Mapping[str, Any],
    backup: Mapping[str, Any],
    source: str,
    filter_args: Sequence[str] = (),
) -> dict[str, Any]:
    settings = guard_settings(backup, pair)
    if not settings["enabled"] or not is_destructive(pair):
        return {"blocked": False, "enabled": False}
    state = _load_state(database)
    key = _state_key(pair)
    existing = (state["quarantines"] or {}).get(key)
    if isinstance(existing, dict):
        return {**existing, "blocked": True, "enabled": True, "quarantine": existing}
    measurement = measure_path(
        source,
        filter_args=filter_args,
        timeout_seconds=settings["measurement_timeout_seconds"],
    )
    finding = evaluate_anomaly(
        (state["baselines"] or {}).get(key), measurement, settings
    )
    finding.update(
        {
            "enabled": True,
            "pair": str(pair.get("name") or ""),
            "history_key": rclone_history_key(dict(pair)),
            "source": source,
            "measurement": measurement,
        }
    )
    if finding.get("blocked"):
        quarantine = {
            **{
                key: value
                for key, value in finding.items()
                if key not in {"source", "measurement"}
            },
            "detected_at": time.time(),
            "acknowledged": False,
        }
        state["quarantines"][key] = quarantine
        _save_state(database, state)
        database.audit_add(
            "anomaly_quarantined",
            actor="system",
            details={k: v for k, v in quarantine.items() if k != "source"},
        )
        finding["quarantine"] = quarantine
    return finding


def record_anomaly_baseline(
    database: Database,
    *,
    pair: Mapping[str, Any],
    measurement: Mapping[str, Any] | None,
) -> None:
    if not measurement or not measurement.get("ok"):
        return
    state = _load_state(database)
    key = _state_key(pair)
    state["baselines"][key] = {
        "pair": str(pair.get("name") or ""),
        "history_key": rclone_history_key(dict(pair)),
        "count": int(measurement.get("count") or 0),
        "bytes": int(measurement.get("bytes") or 0),
        "measured_at": float(measurement.get("measured_at") or time.time()),
    }
    _save_state(database, state)


def anomaly_status(
    database: Database, pairs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    state = _load_state(database)
    by_key = {_state_key(pair): pair for pair in pairs}
    items = []
    for key, quarantine in (state.get("quarantines") or {}).items():
        if key not in by_key or not isinstance(quarantine, dict):
            continue
        items.append(
            {
                field: value
                for field, value in quarantine.items()
                if field not in {"source", "measurement"}
            }
        )
    items.sort(key=lambda item: float(item.get("detected_at") or 0), reverse=True)
    return {"active": len(items), "items": items}


def acknowledge_quarantine(database: Database, pair: Mapping[str, Any]) -> bool:
    state = _load_state(database)
    removed = state["quarantines"].pop(_state_key(pair), None)
    if removed is None:
        return False
    _save_state(database, state)
    database.audit_add(
        "anomaly_quarantine_acknowledged",
        actor="web",
        details={"pair": str(pair.get("name") or "")},
    )
    return True


def protection_calendar(
    database: Database, *, days: int, timezone_name: str
) -> list[dict[str, Any]]:
    bounded_days = max(7, min(int(days), 366))
    cutoff = time.time() - bounded_days * 86400
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("UTC")
    buckets: dict[str, dict[str, Any]] = {}
    for job in database.job_iter(limit=10_000):
        started = float(job.get("started_at") or 0)
        if started < cutoff:
            break
        day = datetime.fromtimestamp(started, tz=timezone).date().isoformat()
        bucket = buckets.setdefault(
            day,
            {
                "date": day,
                "total": 0,
                "successful": 0,
                "failed": 0,
                "cancelled": 0,
                "restore_tests": 0,
                "state": "empty",
            },
        )
        bucket["total"] += 1
        status = str(job.get("status") or "").casefold()
        if status == "ok":
            bucket["successful"] += 1
        elif status == "cancelled":
            bucket["cancelled"] += 1
        elif status != "running":
            bucket["failed"] += 1
        if str(job.get("kind") or "") == "restoretest":
            bucket["restore_tests"] += 1
    for bucket in buckets.values():
        if bucket["failed"]:
            bucket["state"] = "error"
        elif bucket["cancelled"]:
            bucket["state"] = "warning"
        elif bucket["successful"]:
            bucket["state"] = "ok"
    return [buckets[key] for key in sorted(buckets, reverse=True)]


def score_components(
    *,
    overview: Mapping[str, Any],
    storage: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    pairs_summary = overview.get("pairs") or {}
    total = max(0, int(pairs_summary.get("total") or 0))
    enabled = max(0, int(pairs_summary.get("enabled") or 0))

    def weighted(value: int, denominator: int, maximum: int) -> int:
        if denominator <= 0:
            return 0
        return round(min(max(value, 0), denominator) / denominator * maximum)

    health = pairs_summary.get("health") or []
    fresh = sum(
        1
        for item in health
        if isinstance(item, Mapping)
        and not item.get("overdue")
        and str(item.get("last_status") or "").casefold() in {"ok", "success"}
    )
    storage_pairs = storage.get("pairs") or []
    restored = sum(
        1
        for item in storage_pairs
        if isinstance(item, Mapping)
        and isinstance(item.get("restore_evidence"), Mapping)
        and item["restore_evidence"].get("state") == "passed"
    )
    config_pairs = [
        item
        for item in ((config.get("backup") or {}).get("pairs") or [])
        if isinstance(item, Mapping) and item.get("enabled", True)
    ]
    shield_units = 0.0
    for pair in config_pairs:
        if not is_destructive(pair) or not pair.get("allow_delete"):
            shield_units += 1
        elif pair.get("max_delete") not in (None, "", -1, "-1"):
            shield_units += 1 if str(pair.get("backup_dir") or "").strip() else 0.75
    shield = round(shield_units / len(config_pairs) * 15) if config_pairs else 0
    components = [
        {"id": "active", "points": weighted(enabled, total, 15), "maximum": 15},
        {
            "id": "scheduled",
            "points": weighted(int(pairs_summary.get("scheduled") or 0), enabled, 15),
            "maximum": 15,
        },
        {"id": "freshness", "points": weighted(fresh, enabled, 25), "maximum": 25},
        {"id": "restore", "points": weighted(restored, total, 30), "maximum": 30},
        {"id": "shield", "points": shield, "maximum": 15},
    ]
    score = sum(item["points"] for item in components)
    state = "ready" if score >= 85 else "review" if score >= 60 else "risk"
    return {"score": score, "state": state, "components": components}


__all__ = [
    "POLICY_PRESETS",
    "acknowledge_quarantine",
    "anomaly_status",
    "evaluate_anomaly",
    "guard_settings",
    "is_destructive",
    "measure_path",
    "preflight_anomaly_guard",
    "protection_calendar",
    "record_anomaly_baseline",
    "score_components",
]
