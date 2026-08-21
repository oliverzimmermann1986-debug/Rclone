"""Persistierte Jobdefinitionen und sichere Legacy-Migration.

``backup.pairs`` bleiben aus Kompatibilitätsgründen die Datenwege. Zeitplanung
gehört ausschließlich in ``backup.jobs``. Die Helfer in diesem Modul arbeiten
sowohl mit einem Config-Objekt als auch mit einem rohen Mapping, damit Web,
Scheduler und Tests dieselbe effektive Sicht verwenden.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

DEFAULT_SCHEDULE = "0 3 * * *"
DEFAULT_RETRY_MINUTES = 60
_DISABLED_SCHEDULES = {"", "off", "manual", "disabled", "none"}


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein", ""}:
        return False
    return default


def _snapshot(config: Any) -> dict[str, Any]:
    snapshot = getattr(config, "snapshot", None)
    if callable(snapshot):
        value = snapshot()
    elif isinstance(config, Mapping):
        value = config
    else:
        getter = getattr(config, "get", None)
        value = {"backup": getter("backup", default={}) if callable(getter) else {}}
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def stable_job_id(name: str, data_path_ids: list[str]) -> str:
    identity = "\0".join(
        ["rclone-job", str(name).strip().casefold(), *map(str, data_path_ids)]
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, identity).hex


def stable_data_path_id(pair: Mapping[str, Any]) -> str:
    existing = str(pair.get("id") or "").strip().lower()
    if existing:
        return existing
    direction = str(pair.get("direction") or "bisync").strip().lower()
    mode = (
        str(pair.get("mode") or ("bisync" if direction == "bisync" else "copy"))
        .strip()
        .lower()
    )
    if direction == "bisync":
        mode = "bisync"
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        "\0".join(
            (
                "rclone",
                str(pair.get("name") or "").strip().casefold(),
                str(pair.get("remote") or "").strip(),
                str(pair.get("local") or "").strip(),
                direction,
                mode,
            )
        ),
    ).hex


def legacy_job_definitions(backup: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Leitet deterministisch je Legacy-Datenweg genau einen Job ab."""

    default_schedule = str(backup.get("default_schedule") or DEFAULT_SCHEDULE).strip()
    try:
        retry_minutes = int(backup.get("scheduler_retry_minutes") or 60)
    except (TypeError, ValueError):
        retry_minutes = DEFAULT_RETRY_MINUTES
    retry_minutes = max(1, min(retry_minutes, 10080))
    result: list[dict[str, Any]] = []
    for raw in backup.get("pairs") or []:
        if not isinstance(raw, Mapping):
            continue
        pair_id = stable_data_path_id(raw)
        name = str(raw.get("name") or "").strip()
        schedule = str(raw.get("schedule") or "").strip() or default_schedule
        result.append(
            {
                "id": stable_job_id(name, [pair_id]),
                "name": name,
                "enabled": raw.get("enabled", True),
                "data_path_ids": [pair_id],
                "schedule": schedule,
                "execution_mode": "sequential",
                "max_parallel": 1,
                "retry_minutes": retry_minutes,
            }
        )
    return result


def effective_job_definitions(config: Any) -> list[dict[str, Any]]:
    """Liefert persistierte Jobs oder eine nur lesende Legacy-Ableitung.

    Ein ausdrücklich vorhandenes ``jobs: []`` wird niemals aufgefüllt. Das ist
    wichtig, damit ein Administrator alle automatischen Jobs sicher entfernen
    kann, ohne dass der nächste Scheduler-Tick sie neu erfindet.
    """

    backup = _snapshot(config).get("backup") or {}
    if not isinstance(backup, Mapping):
        return []
    if "jobs" in backup:
        jobs = backup.get("jobs")
        return [
            copy.deepcopy(dict(job)) for job in jobs or [] if isinstance(job, Mapping)
        ]
    return legacy_job_definitions(backup)


def data_paths_by_id(config: Any) -> dict[str, dict[str, Any]]:
    backup = _snapshot(config).get("backup") or {}
    pairs = backup.get("pairs") if isinstance(backup, Mapping) else []
    return {
        stable_data_path_id(pair): copy.deepcopy(dict(pair))
        for pair in pairs or []
        if isinstance(pair, Mapping)
    }


def definition_history_key(definition: Mapping[str, Any]) -> str:
    return f"jobdef:id:{str(definition.get('id') or '').strip()}"


def scheduled_data_path_ids(config: Any) -> set[str]:
    """Datenwege mit mindestens einer aktiven, automatischen Jobdefinition."""

    scheduled: set[str] = set()
    for definition in effective_job_definitions(config):
        if not _as_bool(definition.get("enabled", True), default=True):
            continue
        schedule = str(definition.get("schedule") or "manual").strip().casefold()
        if schedule in _DISABLED_SCHEDULES:
            continue
        scheduled.update(
            str(value or "").strip()
            for value in definition.get("data_path_ids") or []
            if str(value or "").strip()
        )
    return scheduled


def definition_pairs(
    config: Any, definition: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Löst Datenwege in der expliziten Reihenfolge der Definition auf."""

    indexed = data_paths_by_id(config)
    return [
        indexed[path_id]
        for path_id in (
            str(value or "").strip() for value in definition.get("data_path_ids") or []
        )
        if path_id in indexed
    ]
