"""Bandbreiten-Heatmap und Zeitplan-Vorschläge aus der Lauf-Historie.

Nur lesend: Es wird nichts geschrieben, Vorschläge übernimmt der Nutzer selbst.
Wochentag-Index 0 = Montag, Slot = (Wochentag, Stunde) in der Zeitzone des Dienstes.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from croniter import croniter

from .job_definitions import effective_job_definitions
from .jobs.scheduler import _is_disabled, _is_valid_schedule, _timezone

MIN_DURATION_SEC = 30  # kürzere Läufe messen Anlaufzeit, keine Bandbreite
NIGHT_BONUS = 0.25  # Score ist relativ zum besten Slot (0..1)
BUSY_MALUS = 0.5
_WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
_SIZE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:([kmgt])i?b?|(b))$", re.IGNORECASE)


def parse_size(text: Any) -> Optional[int]:
    """'1,5 GiB', '12M', '0 B' -> Bytes (1024er-Basis wie rclone); ohne Einheit None."""
    match = _SIZE_RE.match(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    exponent = " kmgt".index((match.group(2) or " ").lower())
    return round(float(match.group(1).replace(",", ".")) * 1024**exponent)


def parse_rate(text: Any) -> Optional[float]:
    """'12.345 MiB/s' -> Bytes/s."""
    if not isinstance(text, str):
        return None
    size = parse_size(re.sub(r"/s$", "", text.strip(), flags=re.IGNORECASE))
    return None if size is None else float(size)


def _is_night(hour: int) -> bool:
    return hour >= 22 or hour < 6


def slot_label(weekday: int, hour: int) -> str:
    return f"{_WEEKDAYS[weekday]} {hour:02d}:00–{(hour + 1) % 24:02d}:00"


def heatmap(
    db: Any,
    *,
    days: int = 90,
    timezone_name: str = "Europe/Berlin",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Mittlerer Durchsatz (Bytes/s) je Wochentag x Startstunde erfolgreicher Läufe."""
    tz = _timezone(timezone_name)
    now_ts = time.time() if now is None else float(now)
    with db.conn() as connection:
        rows = connection.execute(
            "SELECT started_at, ended_at, result_json FROM pair_runs "
            "WHERE ok=1 AND dry_run=0 AND ended_at IS NOT NULL "
            "AND ended_at-started_at>=? AND started_at>=?",
            (MIN_DURATION_SEC, now_ts - days * 86400),
        ).fetchall()
    sums = [[0.0] * 24 for _ in range(7)]
    samples = [[0] * 24 for _ in range(7)]
    for row in rows:
        try:
            pair = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError):
            continue
        size = parse_size(pair.get("transferred")) if isinstance(pair, dict) else None
        if not size:  # 0 B (nichts zu tun) sagt nichts über die Bandbreite
            continue
        start = datetime.fromtimestamp(row["started_at"], tz)
        sums[start.weekday()][start.hour] += size / (
            row["ended_at"] - row["started_at"]
        )
        samples[start.weekday()][start.hour] += 1
    cells = [
        [round(s / n, 1) if n else None for s, n in zip(sum_row, n_row)]
        for sum_row, n_row in zip(sums, samples)
    ]
    return {"cells": cells, "samples": samples, "days": days, "timezone": tz.key}


def _job_slots(schedule: str, start: datetime) -> set[tuple[int, int]]:
    end = start + timedelta(days=7)
    slots: set[tuple[int, int]] = set()
    runs = croniter(schedule, start)
    while (run := runs.get_next(datetime)) < end:
        slots.add((run.weekday(), run.hour))
    return slots


def _cron_jobs(
    cfg: Any, start: datetime
) -> Iterator[tuple[dict[str, Any], str, set[tuple[int, int]]]]:
    """Aktivierte Jobdefinitionen mit gültigem Cron-Zeitplan und ihren Slots."""
    for definition in effective_job_definitions(cfg):
        schedule = " ".join(str(definition.get("schedule") or "").split())
        if (
            definition.get("enabled", True)
            and not _is_disabled(schedule)
            and _is_valid_schedule(schedule)
        ):
            yield definition, schedule, _job_slots(schedule, start)


def scheduled_slots(
    cfg: Any, *, timezone_name: str, now: Optional[float] = None
) -> set[tuple[int, int]]:
    """Slots, in denen aktivierte Jobs in den nächsten 7 Tagen starten."""
    start = datetime.fromtimestamp(
        time.time() if now is None else float(now), _timezone(timezone_name)
    )
    return set().union(*(slots for _, _, slots in _cron_jobs(cfg, start)))


def _scores(hm: dict[str, Any], busy: set) -> dict[tuple[int, int], float]:
    """Relativer Score je Slot; ungemessene Slots erhalten den Median der gemessenen."""
    cells = hm["cells"]
    measured = [value for row in cells for value in row if value is not None]
    top = max(measured, default=0.0)
    fill = statistics.median(measured) if measured else 0.0
    return {
        (day, hour): (
            (
                (fill if cells[day][hour] is None else cells[day][hour]) / top
                if top
                else 0.0
            )
            + (NIGHT_BONUS if _is_night(hour) else 0.0)
            - (BUSY_MALUS if (day, hour) in busy else 0.0)
        )
        for day in range(7)
        for hour in range(24)
    }


def suggest_windows(
    hm: dict[str, Any], busy: set, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Beste Zeitfenster: hoher Durchsatz, Nachtbonus, Malus für belegte Slots."""
    ranked = sorted(_scores(hm, busy).items(), key=lambda item: (-item[1], item[0]))
    return [
        {
            "weekday": day,
            "hour": hour,
            "score": round(score, 3),
            "label": slot_label(day, hour),
        }
        for (day, hour), score in ranked[:limit]
    ]


def propose_schedules(
    cfg: Any, db: Any, *, timezone_name: str, now: Optional[float] = None
) -> list[dict[str, Any]]:
    """Je Job das bessere Fenster als Cron; nur die Stunde ändert sich.

    shortcut: nur Zeitpläne mit fester Stunde und ohne Tag-/Monat-Einschränkung
    ("M H * * W"); Listen/Schritte ("*/6", "2,14") bleiben unangetastet, weil
    eine einzelne Stunde die Häufigkeit senken würde. Upgrade, wenn Nutzer das brauchen.
    """
    now_ts = time.time() if now is None else float(now)
    start = datetime.fromtimestamp(now_ts, _timezone(timezone_name))
    hm = heatmap(db, timezone_name=timezone_name, now=now_ts)
    jobs = list(_cron_jobs(cfg, start))
    slots = [own for _, _, own in jobs]  # vorgeschlagene Slots ersetzen die alten
    proposals: list[dict[str, Any]] = []
    for index, (definition, schedule, own) in enumerate(jobs):
        minute, hour, day_of_month, month, day_of_week = schedule.split()
        if not hour.isdigit() or day_of_month != "*" or month != "*" or not own:
            continue
        busy = set().union(*(s for i, s in enumerate(slots) if i != index))
        scores = _scores(hm, busy)
        days = sorted({day for day, _ in own})
        average = [sum(scores[day, h] for day in days) / len(days) for h in range(24)]
        best = max(range(24), key=average.__getitem__)
        if average[best] <= average[int(hour)]:
            continue
        weekday = max(days, key=lambda day: scores[day, best])
        rate = hm["cells"][weekday][best]
        reason = ", ".join(
            [
                f"Ø {rate / 2**20:.1f} MiB/s gemessen".replace(".", ",")
                if rate is not None
                else "keine Messung",
                *(["Nachtfenster"] if _is_night(best) else []),
                "von anderem Zeitplan belegt" if (weekday, best) in busy else "frei",
            ]
        )
        slots[index] = {(day, best) for day in days}
        proposals.append(
            {
                "definition_id": str(definition.get("id") or ""),
                "name": str(definition.get("name") or ""),
                "current_schedule": schedule,
                "proposed_schedule": (
                    f"{minute if minute.isdigit() else 0} {best} * * {day_of_week}"
                ),
                "weekday": weekday,
                "hour": best,
                "reason": reason,
            }
        )
    return proposals
