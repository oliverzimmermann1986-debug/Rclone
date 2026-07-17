"""Persistente Steuerung für geplante Scheduler-Läufe."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database, get_db

_PAUSE_KEY = "scheduler_pause"
_MAX_PAUSE_SECONDS = 31 * 24 * 3600


def scheduler_state(
    db: Database | None = None, *, now: float | None = None
) -> dict[str, Any]:
    database = db or get_db()
    now_value = float(time.time() if now is None else now)
    raw = database.runtime_get(_PAUSE_KEY, {})
    if not isinstance(raw, dict):
        raw = {}
    until = float(raw.get("until") or 0)
    if until and until <= now_value:
        database.runtime_delete(_PAUSE_KEY)
        raw = {}
        until = 0
    paused = bool(raw.get("paused") and (until <= 0 or until > now_value))
    return {
        "paused": paused,
        "until": until or None,
        "remaining_seconds": max(0, int(until - now_value))
        if paused and until
        else None,
        "reason": str(raw.get("reason") or "").strip()[:300],
        "actor": str(raw.get("actor") or "system").strip()[:128],
        "updated_at": float(raw.get("updated_at") or 0) or None,
    }


def pause_scheduler(
    *,
    until: float | None = None,
    seconds: int | None = None,
    reason: str = "",
    actor: str = "system",
    db: Database | None = None,
) -> dict[str, Any]:
    database = db or get_db()
    now = time.time()
    if until is None:
        duration = max(60, min(int(seconds or 3600), _MAX_PAUSE_SECONDS))
        until = now + duration
    until = float(until)
    if until <= now:
        raise ValueError("Pause-Ende muss in der Zukunft liegen")
    if until - now > _MAX_PAUSE_SECONDS:
        raise ValueError("Scheduler kann höchstens 31 Tage pausiert werden")
    payload = {
        "paused": True,
        "until": until,
        "reason": str(reason or "Wartungsfenster").strip()[:300],
        "actor": str(actor or "system").strip()[:128],
        "updated_at": now,
    }
    database.runtime_set(_PAUSE_KEY, payload)
    database.audit_add(
        "scheduler_paused",
        actor=payload["actor"],
        details={"until": until, "reason": payload["reason"]},
    )
    return scheduler_state(database, now=now)


def resume_scheduler(
    *, actor: str = "system", db: Database | None = None
) -> dict[str, Any]:
    database = db or get_db()
    previous = scheduler_state(database)
    database.runtime_delete(_PAUSE_KEY)
    database.audit_add(
        "scheduler_resumed",
        actor=str(actor or "system")[:128],
        details={
            "previous_until": previous.get("until"),
            "reason": previous.get("reason"),
        },
    )
    return scheduler_state(database)


def pause_until_tomorrow(hour: int, timezone_name: str) -> float:
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    target = (now + timedelta(days=1)).replace(
        hour=max(0, min(hour, 23)), minute=0, second=0, microsecond=0
    )
    return target.timestamp()
