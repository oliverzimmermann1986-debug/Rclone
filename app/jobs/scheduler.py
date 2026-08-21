"""Per-Pair-Scheduler mit Zeitzone, Catch-up und Fehler-Backoff."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from croniter import croniter

from ..job_definitions import (
    data_paths_by_id,
    definition_history_key,
    effective_job_definitions,
)
from ..utils import bounded_int as _bounded_int

logger = logging.getLogger(__name__)

DEFAULT_GLOBAL_SCHEDULE = "0 3 * * *"
DEFAULT_TIMEZONE = "Europe/Berlin"
DISABLED_VALUES = {"", "off", "manual", "disabled", "none"}


def _is_disabled(schedule: Optional[str]) -> bool:
    return not schedule or schedule.strip().lower() in DISABLED_VALUES


def _is_valid_schedule(schedule: str) -> bool:
    """Der Dienst unterstützt bewusst nur minutenbasierte 5-Feld-Cron-Ausdrücke."""
    return len(str(schedule or "").split()) == 5 and croniter.is_valid(schedule)


def _timezone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        logger.warning(
            "Ungültige Scheduler-Zeitzone %r; fallback %s", name, DEFAULT_TIMEZONE
        )
        return ZoneInfo(DEFAULT_TIMEZONE)


def _digest_identity(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{kind}:fp:{hashlib.sha256(encoded).hexdigest()[:24]}"


def rclone_history_key(pair: Mapping[str, Any]) -> str:
    stable_id = str(pair.get("id") or "").strip()
    if stable_id:
        return f"rclone:id:{stable_id}"
    remote = str(pair.get("remote") or "").strip().rstrip("/")
    local = str(pair.get("local") or "").strip().rstrip("/")
    if remote or local:
        return _digest_identity(
            "rclone",
            {
                "remote": remote,
                "local": local,
                "direction": str(pair.get("direction") or "bisync").strip().lower(),
                "mode": str(pair.get("mode") or "bisync").strip().lower(),
            },
        )
    return f"rclone:name:{str(pair.get('name') or '').strip().casefold()}"


def pbs_history_key(settings: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    stable_id = str(target.get("id") or "").strip()
    if stable_id:
        return f"pbs:id:{stable_id}"
    paths = sorted(
        str(path).strip().rstrip("/")
        for path in (target.get("paths") or [])
        if str(path).strip()
    )
    if paths:
        return _digest_identity(
            "pbs",
            {
                "repository": str(settings.get("repository") or "").strip(),
                "namespace": str(
                    target.get("namespace") or settings.get("namespace") or ""
                ).strip(),
                "backup_id": str(
                    target.get("backup_id") or settings.get("backup_id") or ""
                ).strip(),
                "paths": paths,
            },
        )
    return f"pbs:name:{str(target.get('name') or '').strip().casefold()}"


def _load_history(
    db, identities: Mapping[str, str]
) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    bulk = getattr(db, "pair_last_history", None)
    if callable(bulk):
        return bulk(identities)

    # Rückwärtskompatibilität für externe DB-Adapter; die produktive Database
    # nutzt stets den Bulk-Pfad und damit eine einzelne Verbindung.
    result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
    for history_key, pair_name in identities.items():
        result[history_key] = {
            "last_success": db.pair_last_success(pair_name),
            "last_result": db.pair_last_result(pair_name),
        }
    return result


def _next_after(schedule: str, after: float, tz: ZoneInfo) -> float:
    base = datetime.fromtimestamp(after, tz=tz)
    return croniter(schedule, base).get_next(datetime).timestamp()


def _previous_before(schedule: str, now: float, tz: ZoneInfo) -> float:
    base = datetime.fromtimestamp(now, tz=tz)
    return croniter(schedule, base).get_prev(datetime).timestamp()


def _slot_key(
    schedule: str, occurrence: float, tz: ZoneInfo, timezone_name: str
) -> str:
    # fold wird absichtlich nicht aufgenommen: Beide 02:30-Vorkommen beim
    # Herbst-Fallback sind derselbe lokale Cron-Slot.
    wall_time = datetime.fromtimestamp(occurrence, tz=tz).strftime("%Y-%m-%dT%H:%M")
    canonical_schedule = " ".join(schedule.split())
    return f"v1|{timezone_name}|{canonical_schedule}|{wall_time}"


def _due_slot(
    schedule: str,
    last_run: Optional[float],
    now: float,
    *,
    timezone_name: str,
    run_on_first_tick: bool,
    first_run_grace_minutes: int,
) -> tuple[bool, Optional[str], Optional[float]]:
    if not _is_valid_schedule(schedule):
        return False, None, None
    tz = _timezone(timezone_name)
    if last_run is None:
        occurrence = _previous_before(schedule, now + 1, tz)
        due = run_on_first_tick or (
            0 <= now - occurrence <= max(1, first_run_grace_minutes) * 60
        )
    else:
        occurrence = _next_after(schedule, float(last_run), tz)
        due = now >= occurrence
    if not due:
        return False, None, occurrence
    return True, _slot_key(schedule, occurrence, tz, timezone_name), occurrence


def _evaluate_due(
    schedule: str,
    history: Mapping[str, Optional[Dict[str, Any]]],
    *,
    history_key: str,
    now: float,
    timezone_name: str,
    retry_sec: int,
    grace_minutes: int,
    run_on_first_tick: bool,
) -> Dict[str, Any]:
    last_success_result = history.get("last_success") or {}
    last_attempt = history.get("last_result") or {}
    last_success = (
        float(last_success_result["ended_at"])
        if last_success_result.get("ended_at")
        else None
    )
    attempt_ts = float(
        last_attempt.get("ended_at") or last_attempt.get("started_at") or 0
    )
    summary_result = last_attempt.get("summary") or {}
    pair_result = last_attempt.get("pair") or {}
    attempt_context = summary_result if isinstance(summary_result, dict) else {}
    if not attempt_context:
        attempt_context = pair_result if isinstance(pair_result, dict) else {}
    scheduled_failure = bool(
        last_attempt
        and not last_attempt.get("ok")
        and (
            str(last_attempt.get("trigger") or "") == "scheduler"
            or attempt_context.get("trigger") == "scheduler"
            or (
                bool(last_attempt.get("definition_id"))
                and bool(last_attempt.get("scheduled_slot"))
            )
        )
        and attempt_ts > (last_success or 0)
    )
    retry_due = scheduled_failure and now - attempt_ts >= retry_sec
    if scheduled_failure and not retry_due:
        return {
            "due": False,
            "last_run": last_success,
            "last_attempt": attempt_ts,
            "reason": "retry_backoff",
            "retry_at": attempt_ts + retry_sec,
        }

    schedule_due, scheduled_slot, scheduled_at = _due_slot(
        schedule,
        last_success,
        now,
        timezone_name=timezone_name,
        run_on_first_tick=run_on_first_tick,
        first_run_grace_minutes=grace_minutes,
    )
    previous_slot = ""
    if isinstance(attempt_context, dict):
        previous_slot = str(attempt_context.get("scheduled_slot") or "")
    previous_slot = previous_slot or str(last_attempt.get("scheduled_slot") or "")

    duplicate_slot = bool(
        schedule_due
        and scheduled_slot
        and previous_slot == scheduled_slot
        and attempt_ts
        and scheduled_at
    )
    if duplicate_slot:
        tz = _timezone(timezone_name)
        next_distinct_at = _next_after(schedule, float(scheduled_at), tz)
        next_distinct_slot = _slot_key(schedule, next_distinct_at, tz, timezone_name)
        latest_same_slot_at = float(scheduled_at)
        repeated_absolute_slot = False
        # croniter kann bei einer Rückstellung mehr als ein absolutes Vorkommen
        # desselben lokalen Slots liefern. Überspringe alle, aber höchstens die
        # wenigen möglichen Offset-Folds.
        for _ in range(3):
            if next_distinct_slot != scheduled_slot:
                break
            repeated_absolute_slot = True
            latest_same_slot_at = next_distinct_at
            next_distinct_at = _next_after(schedule, next_distinct_at, tz)
            next_distinct_slot = _slot_key(
                schedule, next_distinct_at, tz, timezone_name
            )
        retry_after_repeated_minute = bool(
            retry_due and repeated_absolute_slot and now >= latest_same_slot_at + 60
        )
        if retry_due and (not repeated_absolute_slot or retry_after_repeated_minute):
            schedule_due = False
        elif now < next_distinct_at:
            return {
                "due": False,
                "last_run": last_success,
                "last_attempt": attempt_ts,
                "scheduled_at": scheduled_at,
                "scheduled_slot": scheduled_slot,
                "reason": "slot_already_attempted",
            }
        else:
            schedule_due = True
            scheduled_at = next_distinct_at
            scheduled_slot = next_distinct_slot

    if retry_due:
        source = last_attempt.get("job_id") or f"{attempt_ts:.6f}"
        slot = f"retry|{history_key}|{source}"
        return {
            "due": True,
            "last_run": last_success,
            "last_attempt": attempt_ts,
            "scheduled_slot": slot,
            "reason": "retry_after_failure",
        }

    result: Dict[str, Any] = {
        "due": schedule_due,
        "last_run": last_success,
        "scheduled_at": scheduled_at,
        "scheduled_slot": scheduled_slot,
    }
    if last_success is None and not schedule_due:
        result["reason"] = "waiting_for_first_schedule"
    return result


def _is_due(
    schedule: str,
    last_run: Optional[float],
    now: Optional[float] = None,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    run_on_first_tick: bool = False,
    first_run_grace_minutes: int = 15,
) -> bool:
    if not _is_valid_schedule(schedule):
        logger.warning("Ungültige Cron-Expression: %r", schedule)
        return False
    now_value = float(time.time() if now is None else now)
    due, _, _ = _due_slot(
        schedule,
        last_run,
        now_value,
        timezone_name=timezone_name,
        run_on_first_tick=run_on_first_tick,
        first_run_grace_minutes=first_run_grace_minutes,
    )
    return due


def _load_job_history(
    db, definitions: Mapping[str, str]
) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    bulk = getattr(db, "job_definition_history", None)
    if callable(bulk):
        return bulk(definitions)
    # Adapter für ältere DB-Testdoubles und externe Integrationen. Der
    # produktive Database-Pfad verwendet ausschließlich Job-Run-Historie.
    return _load_history(
        db,
        {
            f"jobdef:id:{definition_id}": name
            for definition_id, name in definitions.items()
        },
    )


def find_due_jobs(
    cfg, db, *, now: Optional[float] = None
) -> Tuple[List[str], List[Dict]]:
    """Bewertet persistierte Jobdefinitionen, niemals Pair-Zeitpläne."""

    backup = cfg.get("backup") or {}
    timezone_name = str(backup.get("timezone") or DEFAULT_TIMEZONE)
    grace_minutes = _bounded_int(
        backup.get("scheduler_grace_minutes", 15),
        default=15,
        minimum=1,
        maximum=1440,
    )
    run_on_first_tick = bool(backup.get("run_on_first_tick", False))
    now_value = float(time.time() if now is None else now)

    definitions = effective_job_definitions(cfg)
    paths = data_paths_by_id(cfg)
    histories = _load_job_history(
        db,
        {
            str(definition.get("id") or ""): str(definition.get("name") or "?")
            for definition in definitions
            if str(definition.get("id") or "")
        },
    )

    due: List[str] = []
    status: List[Dict] = []
    for definition in definitions:
        definition_id = str(definition.get("id") or "").strip()
        name = str(definition.get("name") or "?")
        history_key = definition_history_key(definition)
        base = {
            "id": definition_id,
            "definition_id": definition_id,
            "name": name,
            "history_key": history_key,
            "data_path_ids": list(definition.get("data_path_ids") or []),
            "execution_mode": definition.get("execution_mode", "sequential"),
            "max_parallel": int(definition.get("max_parallel") or 1),
            "retry_minutes": int(
                definition.get("retry_minutes")
                or backup.get("scheduler_retry_minutes")
                or 60
            ),
        }
        if not definition.get("enabled", True):
            status.append(
                {
                    **base,
                    "due": False,
                    "reason": "disabled",
                }
            )
            continue

        referenced = [paths.get(path_id) for path_id in base["data_path_ids"]]
        if not any(path and path.get("enabled", True) for path in referenced):
            status.append({**base, "due": False, "reason": "no_enabled_data_paths"})
            continue

        schedule = str(definition.get("schedule") or "manual").strip()
        if _is_disabled(schedule):
            status.append(
                {
                    **base,
                    "due": False,
                    "reason": f"schedule={schedule}",
                }
            )
            continue
        if not _is_valid_schedule(schedule):
            status.append(
                {
                    **base,
                    "due": False,
                    "reason": "invalid_schedule",
                    "error": schedule,
                }
            )
            continue

        retry_sec = max(1, min(base["retry_minutes"], 10080)) * 60
        try:
            evaluation = _evaluate_due(
                schedule,
                histories.get(definition_id) or histories.get(history_key) or {},
                history_key=history_key,
                now=now_value,
                timezone_name=timezone_name,
                retry_sec=retry_sec,
                grace_minutes=grace_minutes,
                run_on_first_tick=run_on_first_tick,
            )
            next_run = next_run_after(
                schedule, after=now_value, timezone_name=timezone_name
            )
        except Exception as exc:
            status.append(
                {
                    **base,
                    "due": False,
                    "error": str(exc),
                }
            )
            continue

        item = {
            **base,
            "schedule": schedule,
            "timezone": timezone_name,
            "next_run": next_run,
            **evaluation,
        }
        status.append(item)
        if item["due"]:
            due.append(name)
    return due, status


def find_due_pairs(
    cfg, db, *, now: Optional[float] = None
) -> Tuple[List[str], List[Dict]]:
    """Kompatibilitätsname; die kanonische Auswertung erfolgt pro Job."""

    return find_due_jobs(cfg, db, now=now)


def find_due_pbs_targets(
    cfg, db, *, now: Optional[float] = None
) -> Tuple[List[str], List[Dict]]:
    """Fällige PBS-Targets analog zu find_due_pairs.

    Die Läufe werden als pair_runs mit Prefix "pbs:" persistiert, wodurch
    dieselbe last_success-/Retry-Mechanik greift. Backoff und Nachholfenster
    kommen aus der backup-Sektion, damit es nur eine Stellschraube gibt.
    """
    from .pbs_backup import PAIR_PREFIX, client_path, pbs_settings, pbs_targets

    settings = pbs_settings(cfg)
    if not bool(settings.get("enabled", False)):
        return [], []
    if not client_path():
        return [], [{"name": "pbs", "due": False, "reason": "client_not_installed"}]

    backup = cfg.get("backup") or {}
    timezone_name = str(backup.get("timezone") or DEFAULT_TIMEZONE)
    retry_sec = (
        _bounded_int(
            backup.get("scheduler_retry_minutes", 60),
            default=60,
            minimum=1,
            maximum=10080,
        )
        * 60
    )
    grace_minutes = _bounded_int(
        backup.get("scheduler_grace_minutes", 15), default=15, minimum=1, maximum=1440
    )
    now_value = float(time.time() if now is None else now)

    prepared = []
    for target in pbs_targets(settings):
        name = str(target.get("name") or "?")
        run_name = f"{PAIR_PREFIX}{name}"
        prepared.append((target, name, run_name, pbs_history_key(settings, target)))
    histories = _load_history(
        db, {history_key: run_name for _, _, run_name, history_key in prepared}
    )

    due: List[str] = []
    status: List[Dict] = []
    for target, name, run_name, history_key in prepared:
        schedule = str(target.get("schedule") or "manual").strip()
        if _is_disabled(schedule):
            status.append(
                {
                    "name": name,
                    "run_name": run_name,
                    "history_key": history_key,
                    "due": False,
                    "reason": f"schedule={schedule}",
                }
            )
            continue
        if not _is_valid_schedule(schedule):
            status.append(
                {
                    "name": name,
                    "run_name": run_name,
                    "history_key": history_key,
                    "due": False,
                    "reason": "invalid_schedule",
                }
            )
            continue

        try:
            evaluation = _evaluate_due(
                schedule,
                histories.get(history_key) or {},
                history_key=history_key,
                now=now_value,
                timezone_name=timezone_name,
                retry_sec=retry_sec,
                grace_minutes=grace_minutes,
                run_on_first_tick=False,
            )
        except Exception as exc:
            status.append(
                {
                    "name": name,
                    "run_name": run_name,
                    "history_key": history_key,
                    "due": False,
                    "error": str(exc),
                }
            )
            continue
        item = {
            "name": name,
            "run_name": run_name,
            "history_key": history_key,
            "schedule": schedule,
            "timezone": timezone_name,
            **evaluation,
        }
        status.append(item)
        if item["due"]:
            due.append(name)
    return due, status


RESTORE_TEST_HISTORY_KEY = "restoretest:global"


def restore_test_due(cfg, db, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Fälligkeit des Restore-Drills.

    Der Drill läuft als ein Lauf über alle Pairs, nicht pro Pair — sonst
    konkurrierten mehrere Drills um denselben Backup-Scope. Historie und
    Retry-Backoff nutzen darum einen einzigen globalen Schlüssel.
    """
    from .restore_test import restore_test_settings

    settings = restore_test_settings(cfg)
    now_value = float(time.time() if now is None else now)
    if not settings["enabled"]:
        return {"due": False, "reason": "disabled"}
    schedule = str(settings["schedule"] or "manual").strip()
    if _is_disabled(schedule):
        return {"due": False, "reason": f"schedule={schedule}"}
    if not _is_valid_schedule(schedule):
        return {"due": False, "reason": "invalid_schedule", "error": schedule}

    if isinstance(cfg, Mapping):
        backup = cfg.get("backup") or {}
    else:
        backup = cfg.get("backup", default={}) or {}
    timezone_name = str(backup.get("timezone") or DEFAULT_TIMEZONE)
    retry_sec = (
        _bounded_int(
            backup.get("scheduler_retry_minutes", 60),
            default=60,
            minimum=1,
            maximum=10080,
        )
        * 60
    )
    grace_minutes = _bounded_int(
        backup.get("scheduler_grace_minutes", 15), default=15, minimum=1, maximum=1440
    )
    history = _load_history(db, {RESTORE_TEST_HISTORY_KEY: RESTORE_TEST_HISTORY_KEY})
    try:
        evaluation = _evaluate_due(
            schedule,
            history.get(RESTORE_TEST_HISTORY_KEY) or {},
            history_key=RESTORE_TEST_HISTORY_KEY,
            now=now_value,
            timezone_name=timezone_name,
            retry_sec=retry_sec,
            grace_minutes=grace_minutes,
            run_on_first_tick=False,
        )
    except Exception as exc:
        return {"due": False, "error": str(exc)}
    return {
        "history_key": RESTORE_TEST_HISTORY_KEY,
        "schedule": schedule,
        "timezone": timezone_name,
        "next_run": next_run_after(
            schedule, after=now_value, timezone_name=timezone_name
        ),
        **evaluation,
    }


def next_run_after(
    schedule: str,
    *,
    after: Optional[float] = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> Optional[float]:
    if _is_disabled(schedule) or not _is_valid_schedule(schedule):
        return None
    tz = _timezone(timezone_name)
    base = datetime.fromtimestamp(float(time.time() if after is None else after), tz=tz)
    return croniter(schedule, base).get_next(datetime).timestamp()
