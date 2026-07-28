"""Ausbleib-Erkennung: Pairs ohne frischen Erfolg melden.

Ein fehlgeschlagener Lauf erzeugt bereits eine Benachrichtigung. Ein Lauf, der
gar nicht erst startet — deaktivierter Timer, toter Scheduler, gestoppter
Container — erzeugte bisher Stille. Dieses Modul schließt die Lücke und ist die
gemeinsame Quelle für die Übersichts-API und den Scheduler-Tick.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Mapping, Optional

from .utils import bounded_int as _bounded_int

logger = logging.getLogger(__name__)

# Debounce-Zustand liegt in runtime_settings, damit ein Neustart nicht sofort
# erneut alarmiert.
_STATE_KEY = "overdue_alerts"
_MAX_TRACKED = 512


def alert_settings(cfg) -> dict[str, Any]:
    backup = cfg.get("backup", default={}) or {}
    raw = backup.get("overdue_alerts")
    if not isinstance(raw, Mapping):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "repeat_hours": _bounded_int(
            raw.get("repeat_hours", 24), default=24, minimum=1, maximum=720
        ),
    }


def evaluate_pair(
    pair: Mapping[str, Any],
    last_success_at: Optional[float],
    *,
    now: float,
) -> dict[str, Any]:
    """Fälligkeitsurteil für ein einzelnes Pair.

    ``max_success_age_hours <= 0`` schaltet die Prüfung für dieses Pair ab.
    Ein Pair ohne jeden erfolgreichen Lauf gilt als überfällig, sobald eine
    Frist gesetzt ist — genau der Fall "hat noch nie funktioniert".
    """
    try:
        max_age_hours = float(pair.get("max_success_age_hours") or 0)
    except (TypeError, ValueError):
        max_age_hours = 0.0

    age_hours = (
        max(0.0, (now - float(last_success_at)) / 3600.0)
        if last_success_at is not None
        else None
    )
    overdue = bool(
        max_age_hours > 0 and (age_hours is None or age_hours > max_age_hours)
    )
    return {
        "last_success": last_success_at,
        "success_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "max_success_age_hours": max_age_hours,
        "overdue": overdue,
    }


def _load_state(db) -> dict[str, float]:
    raw = db.runtime_get(_STATE_KEY, {})
    if not isinstance(raw, Mapping):
        return {}
    state: dict[str, float] = {}
    for key, value in raw.items():
        try:
            state[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return state


def _store_state(db, state: Mapping[str, float]) -> None:
    # Historienschlüssel gelöschter Pairs sollen nicht ewig mitwachsen.
    trimmed = dict(
        sorted(state.items(), key=lambda item: item[1], reverse=True)[:_MAX_TRACKED]
    )
    try:
        db.runtime_set(_STATE_KEY, trimmed)
    except ValueError as exc:
        logger.warning("Ausbleib-Zustand nicht speicherbar: %s", exc)


def _describe(item: Mapping[str, Any]) -> str:
    name = str(item.get("name") or "?")
    age = item.get("success_age_hours")
    limit = item.get("max_success_age_hours")
    if age is None:
        return f"{name}: noch kein erfolgreicher Lauf (Frist {limit:g} Std.)"
    return f"{name}: letzter Erfolg vor {age:g} Std. (Frist {limit:g} Std.)"


def notify_overdue(
    cfg,
    db,
    overdue_items: Iterable[Mapping[str, Any]],
    *,
    now: Optional[float] = None,
) -> list[str]:
    """Feuert ``pair_overdue`` für neu bzw. erneut überfällige Pairs.

    Gibt die gemeldeten Pair-Namen zurück. Pairs, die wieder erfolgreich
    liefen, werden aus dem Debounce-Zustand entfernt, damit der nächste
    Ausfall sofort und nicht erst nach ``repeat_hours`` meldet.
    """
    settings = alert_settings(cfg)
    now_value = float(time.time() if now is None else now)
    items = [dict(item) for item in overdue_items]
    state = _load_state(db)

    still_overdue = {
        str(item.get("history_key") or item.get("name") or "") for item in items
    }
    recovered = [key for key in state if key not in still_overdue]
    for key in recovered:
        state.pop(key, None)

    if not settings["enabled"]:
        if recovered:
            _store_state(db, state)
        return []

    repeat_sec = settings["repeat_hours"] * 3600
    reported: list[str] = []
    for item in items:
        key = str(item.get("history_key") or item.get("name") or "")
        if not key:
            continue
        last_sent = state.get(key, 0.0)
        if last_sent and now_value - last_sent < repeat_sec:
            continue
        state[key] = now_value
        reported.append(str(item.get("name") or key))

    if reported or recovered:
        _store_state(db, state)

    if reported:
        # Ein Webhook pro Tick statt einer je Pair — sonst ist der Kanal bei
        # einem toten Scheduler mit identischen Meldungen geflutet.
        from .notifications import notify

        detail = [
            _describe(item)
            for item in items
            if str(item.get("name") or "") in set(reported)
        ]
        notify(
            "pair_overdue",
            f"{len(reported)} Pair(s) ohne frischen erfolgreichen Lauf",
            "\n".join(detail),
            pairs=reported,
        )
    return reported


def is_scheduled(pair: Mapping[str, Any], default_schedule: str) -> bool:
    """Nur geplante Pairs können überfällig werden.

    Ein manuell betriebenes Pair hat keinen erwarteten Zeitpunkt; eine Frist
    darauf anzuwenden würde dauerhaft und grundlos alarmieren.
    """
    from .jobs.scheduler import DISABLED_VALUES

    schedule = str(pair.get("schedule") or "").strip() or default_schedule
    return schedule.strip().casefold() not in DISABLED_VALUES


def check_and_notify(cfg, db, *, now: Optional[float] = None) -> list[str]:
    """Alle geplanten, aktiven Pairs prüfen und bei Bedarf alarmieren."""
    from .jobs.scheduler import rclone_history_key

    now_value = float(time.time() if now is None else now)
    backup = cfg.get("backup", default={}) or {}
    default_schedule = str(backup.get("default_schedule") or "").strip()
    pairs = [
        pair
        for pair in (backup.get("pairs") or [])
        if isinstance(pair, Mapping)
        and pair.get("enabled", True)
        and is_scheduled(pair, default_schedule)
    ]
    if not pairs:
        return notify_overdue(cfg, db, [], now=now_value)

    identities = {
        rclone_history_key(pair): str(pair.get("name") or "")
        for pair in pairs
        if str(pair.get("name") or "")
    }
    histories = db.pair_last_history(identities)

    items: list[dict[str, Any]] = []
    for pair in pairs:
        name = str(pair.get("name") or "")
        if not name:
            continue
        history_key = rclone_history_key(pair)
        success = (histories.get(history_key) or {}).get("last_success") or {}
        last_success_at = (
            float(success["ended_at"]) if success.get("ended_at") else None
        )
        verdict = evaluate_pair(pair, last_success_at, now=now_value)
        if verdict["overdue"]:
            items.append({"name": name, "history_key": history_key, **verdict})
    return notify_overdue(cfg, db, items, now=now_value)
