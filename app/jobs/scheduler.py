"""Per-Pair-Scheduler für Backup-Jobs.

Aufruf:
    python -m app.jobs.scheduler_cli

Läuft minütlich via systemd-Timer. Liest pair.schedule aus der Config,
prüft mit croniter ob seit dem letzten erfolgreichen Run die Cron-Zeit
überschritten wurde, und triggert dann nur die fälligen Pairs.

Schedule-Format ist Standard-Cron (5 Felder):
    "0 3 * * *"      - täglich 03:00
    "*/15 * * * *"   - alle 15 Minuten
    "0 */6 * * *"    - alle 6h zur vollen Stunde
    "0 9 * * 1-5"    - Mo-Fr 09:00

Sonderwert "off" / "manual" / "" / None deaktiviert den Pair.
Wenn pair.schedule fehlt, wird backup.default_schedule verwendet
(Bestandsverhalten = "0 3 * * *" wie der alte Timer).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_GLOBAL_SCHEDULE = "0 3 * * *"  # täglich 3 Uhr - Bestandsverhalten
DISABLED_VALUES = {"", "off", "manual", "disabled", "none"}


def _is_disabled(schedule: Optional[str]) -> bool:
    return not schedule or schedule.strip().lower() in DISABLED_VALUES


def _last_success_ts(db, pair_name: str) -> Optional[float]:
    """Findet den letzten erfolgreichen Sync-Zeitpunkt für einen Pair.
    Schaut in jobs.summary.pairs[].name nach dem letzten 'ok'-Job."""
    rows = db.job_list(kind="backup", limit=200)
    for j in rows:
        if j.get("status") != "ok":
            continue
        summary = j.get("summary") or {}
        for ps in summary.get("pairs") or []:
            if ps.get("name") == pair_name and ps.get("status") in ("ok", "skipped"):
                return j.get("ended_at") or j.get("started_at")
    return None


def _is_due(schedule: str, last_run: Optional[float], now: Optional[float] = None) -> bool:
    """Prüft ob ein Pair laut Schedule jetzt dran ist.

    Logik: nimmt die Cron-Expression, berechnet "wann wäre der nächste Run
    NACH dem letzten Run gewesen" - wenn dieser Zeitpunkt <= jetzt ist, ist
    der Pair überfällig und sollte laufen.

    Wenn last_run=None (noch nie gelaufen), gilt der Pair sofort als fällig.
    """
    from croniter import croniter

    if not croniter.is_valid(schedule):
        logger.warning(f"Ungültige Cron-Expression: {schedule!r}")
        return False

    if last_run is None:
        return True

    now = now or time.time()
    # Erster geplanter Run-Zeitpunkt nach dem letzten erfolgreichen Run
    base = datetime.fromtimestamp(last_run, tz=timezone.utc)
    ci = croniter(schedule, base)
    next_run = ci.get_next(datetime).timestamp()
    return now >= next_run


def find_due_pairs(cfg, db, *, now: Optional[float] = None) -> Tuple[List[str], List[Dict]]:
    """Bestimmt welche Pairs jetzt fällig sind.

    Returnt (due_pair_names, all_pair_status):
        - due_pair_names: ['pair1', 'pair2'] für den nächsten Sync-Call
        - all_pair_status: Liste mit Diagnose-Info pro Pair (für UI/Log)
    """
    backup_cfg = cfg.get("backup") or {}
    pairs = backup_cfg.get("pairs") or []
    default_schedule = (backup_cfg.get("default_schedule") or DEFAULT_GLOBAL_SCHEDULE).strip()

    due: List[str] = []
    status: List[Dict] = []

    for pair in pairs:
        name = pair.get("name") or "?"
        if not pair.get("enabled", True):
            status.append({"name": name, "due": False, "reason": "disabled"})
            continue

        schedule = (pair.get("schedule") or "").strip() or default_schedule
        if _is_disabled(schedule):
            status.append({"name": name, "due": False, "reason": f"schedule={schedule}"})
            continue

        last_run = _last_success_ts(db, name)
        try:
            is_due = _is_due(schedule, last_run, now=now)
        except Exception as e:
            status.append({"name": name, "due": False, "error": str(e)})
            continue

        status.append({
            "name": name, "due": is_due, "schedule": schedule,
            "last_run": last_run,
        })
        if is_due:
            due.append(name)

    return due, status


def next_run_after(schedule: str, *, after: Optional[float] = None) -> Optional[float]:
    """Gibt den nächsten geplanten Run-Zeitpunkt als Unix-Timestamp zurück
    (oder None bei ungültiger/disabled Schedule). Für UI-Anzeige."""
    from croniter import croniter

    if _is_disabled(schedule) or not croniter.is_valid(schedule):
        return None
    base = datetime.fromtimestamp(after or time.time(), tz=timezone.utc)
    return croniter(schedule, base).get_next(datetime).timestamp()
