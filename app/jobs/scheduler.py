"""Per-Pair-Scheduler mit Zeitzone, Catch-up und Fehler-Backoff."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from croniter import croniter

logger = logging.getLogger(__name__)

DEFAULT_GLOBAL_SCHEDULE = "0 3 * * *"
DEFAULT_TIMEZONE = "Europe/Berlin"
DISABLED_VALUES = {"", "off", "manual", "disabled", "none"}


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _is_disabled(schedule: Optional[str]) -> bool:
    return not schedule or schedule.strip().lower() in DISABLED_VALUES


def _timezone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        logger.warning(
            "Ungültige Scheduler-Zeitzone %r; fallback %s", name, DEFAULT_TIMEZONE
        )
        return ZoneInfo(DEFAULT_TIMEZONE)


def _last_success_ts(db, pair_name: str) -> Optional[float]:
    result = db.pair_last_success(pair_name)
    return float(result["ended_at"]) if result and result.get("ended_at") else None


def _last_attempt(db, pair_name: str) -> Optional[Dict]:
    return db.pair_last_result(pair_name)


def _next_after(schedule: str, after: float, tz: ZoneInfo) -> float:
    base = datetime.fromtimestamp(after, tz=tz)
    return croniter(schedule, base).get_next(datetime).timestamp()


def _previous_before(schedule: str, now: float, tz: ZoneInfo) -> float:
    base = datetime.fromtimestamp(now, tz=tz)
    return croniter(schedule, base).get_prev(datetime).timestamp()


def _is_due(
    schedule: str,
    last_run: Optional[float],
    now: Optional[float] = None,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    run_on_first_tick: bool = False,
    first_run_grace_minutes: int = 15,
) -> bool:
    if not croniter.is_valid(schedule):
        logger.warning("Ungültige Cron-Expression: %r", schedule)
        return False
    now_value = float(time.time() if now is None else now)
    tz = _timezone(timezone_name)
    if last_run is None:
        if run_on_first_tick:
            return True
        previous = _previous_before(schedule, now_value + 1, tz)
        return 0 <= now_value - previous <= max(1, first_run_grace_minutes) * 60
    return now_value >= _next_after(schedule, float(last_run), tz)


def find_due_pairs(
    cfg, db, *, now: Optional[float] = None
) -> Tuple[List[str], List[Dict]]:
    backup = cfg.get("backup") or {}
    pairs = backup.get("pairs") or []
    default_schedule = str(
        backup.get("default_schedule") or DEFAULT_GLOBAL_SCHEDULE
    ).strip()
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
        backup.get("scheduler_grace_minutes", 15),
        default=15,
        minimum=1,
        maximum=1440,
    )
    run_on_first_tick = bool(backup.get("run_on_first_tick", False))
    now_value = float(time.time() if now is None else now)

    due: List[str] = []
    status: List[Dict] = []
    for pair in pairs:
        name = str(pair.get("name") or "?")
        if not pair.get("enabled", True):
            status.append({"name": name, "due": False, "reason": "disabled"})
            continue

        schedule = str(pair.get("schedule") or "").strip() or default_schedule
        if _is_disabled(schedule):
            status.append(
                {"name": name, "due": False, "reason": f"schedule={schedule}"}
            )
            continue
        if not croniter.is_valid(schedule):
            status.append(
                {
                    "name": name,
                    "due": False,
                    "reason": "invalid_schedule",
                    "error": schedule,
                }
            )
            continue

        last_success = _last_success_ts(db, name)
        last_attempt = _last_attempt(db, name)
        retry_due = False
        attempt_ts = 0.0
        if last_attempt and not last_attempt.get("ok"):
            attempt_ts = float(
                last_attempt.get("ended_at") or last_attempt.get("started_at") or 0
            )
            pair_result = last_attempt.get("pair") or {}
            scheduled_failure = (
                isinstance(pair_result, dict)
                and pair_result.get("trigger") == "scheduler"
            )
            if attempt_ts > (last_success or 0) and scheduled_failure:
                if now_value - attempt_ts < retry_sec:
                    status.append(
                        {
                            "name": name,
                            "due": False,
                            "schedule": schedule,
                            "last_run": last_success,
                            "last_attempt": attempt_ts,
                            "reason": "retry_backoff",
                            "retry_at": attempt_ts + retry_sec,
                        }
                    )
                    continue
                retry_due = True

        try:
            is_due = retry_due or _is_due(
                schedule,
                last_success,
                now=now_value,
                timezone_name=timezone_name,
                run_on_first_tick=run_on_first_tick,
                first_run_grace_minutes=grace_minutes,
            )
            next_run = next_run_after(
                schedule, after=last_success or now_value, timezone_name=timezone_name
            )
        except Exception as exc:
            status.append({"name": name, "due": False, "error": str(exc)})
            continue

        item = {
            "name": name,
            "due": is_due,
            "schedule": schedule,
            "timezone": timezone_name,
            "last_run": last_success,
            "next_run": next_run,
        }
        if retry_due:
            item["reason"] = "retry_after_failure"
            item["last_attempt"] = attempt_ts
        elif last_success is None and not is_due:
            item["reason"] = "waiting_for_first_schedule"
        status.append(item)
        if is_due:
            due.append(name)
    return due, status


def next_run_after(
    schedule: str,
    *,
    after: Optional[float] = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> Optional[float]:
    if _is_disabled(schedule) or not croniter.is_valid(schedule):
        return None
    tz = _timezone(timezone_name)
    base = datetime.fromtimestamp(float(time.time() if after is None else after), tz=tz)
    return croniter(schedule, base).get_next(datetime).timestamp()
