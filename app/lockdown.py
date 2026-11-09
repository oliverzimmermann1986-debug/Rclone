"""Notfall-Lockdown: stoppt Jobs, pausiert den Scheduler und meldet alle ab."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from typing import Any, Callable

from .config_store import get_config
from .db import Database, get_db
from .push_notifications import send_push_notifications

logger = logging.getLogger(__name__)

_KEY = "lockdown"
_MAX_MOUNTS = 50
_MAX_REPORT_BYTES = 4096
_PAUSE_SECONDS = 31 * 24 * 3600
_DENIED = "keine Berechtigung (CAP_SYS_ADMIN fehlt)"
_DENIED_MARKERS = ("superuser", "only root", "permission denied", "not permitted")


def lockdown_state(db: Database | None = None) -> dict[str, Any]:
    raw = (db or get_db()).runtime_get(_KEY, {})
    if isinstance(raw, dict) and raw.get("active") is True:
        return raw
    return {"active": False}


def is_locked_down(db: Database | None = None) -> bool:
    return lockdown_state(db)["active"]


def _error(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:200]


def _step(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "result": fn(*args, **kwargs)}
    except Exception as exc:
        logger.exception(
            "Lockdown-Schritt %s fehlgeschlagen", getattr(fn, "__name__", fn)
        )
        return {"ok": False, "error": _error(exc)}


def _mountpoints() -> list[str]:
    found: list[str] = []
    for pair in get_config().get("backup", "pairs", default=[]) or []:
        if not isinstance(pair, dict) or not pair.get("enabled", True):
            continue
        # rstrip: "/" fällt so heraus, damit nie das Root-Dateisystem remountet wird.
        mountpoint = str(pair.get("mountpoint") or "").strip().rstrip("/")
        if pair.get("require_mountpoint") and mountpoint.startswith("/"):
            if mountpoint not in found:
                found.append(mountpoint)
    return found[:_MAX_MOUNTS]


def _remount_ro(mountpoint: str, run: Callable[..., Any]) -> dict[str, Any]:
    try:
        result = run(
            ["mount", "-o", "remount,ro", mountpoint],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env={
                **os.environ,
                "LC_ALL": "C",
            },  # englische Meldungen für die Rechteerkennung
        )
        stderr = str(result.stderr or "").strip()
        if result.returncode == 0:
            error = None
        elif any(marker in stderr.lower() for marker in _DENIED_MARKERS):
            error = _DENIED
        else:
            error = (
                stderr.splitlines()[-1] if stderr else f"Exit-Code {result.returncode}"
            )[:200]
    except FileNotFoundError:
        error = "mount-Programm nicht gefunden"
    except subprocess.TimeoutExpired:
        error = "Zeitüberschreitung nach 20 s"
    except Exception as exc:
        error = _error(exc)
    return {"mountpoint": mountpoint, "ok": error is None, "error": error}


def _fit(report: dict[str, Any]) -> None:
    """Kürzt die Remount-Liste auf 4 KB; erfolgreiche Einträge bleiben am längsten."""
    entries = report["remount"]
    while (
        entries
        and len(json.dumps(report, ensure_ascii=False, default=str).encode("utf-8"))
        > _MAX_REPORT_BYTES
    ):
        failed = [index for index, entry in enumerate(entries) if not entry["ok"]]
        entries.pop(failed[-1] if failed else -1)
        report["remount_omitted"] = report.get("remount_omitted", 0) + 1


def _previous_pause(database: Database) -> dict[str, Any] | None:
    from .scheduler_control import scheduler_state

    current = lockdown_state(database)
    if current["active"]:
        # Erneute Aktivierung: die Pause vor dem ersten Lockdown bleibt maßgeblich.
        return current.get("previous_pause")
    state = scheduler_state(database)
    return {key: state[key] for key in ("paused", "until", "reason")}


def activate_lockdown(
    *,
    actor: str,
    reason: str,
    db: Database | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    # Spät importiert, damit Scheduler/Jobs is_locked_down ohne Importzyklus nutzen können.
    from .auth import bump_session_version
    from .jobs import rclone_sync, runtime_state
    from .jobs.pbs_backup import PBS_CANCEL_SCOPE
    from .scheduler_control import pause_scheduler

    database = db or get_db()
    actor = str(actor or "system").strip()[:128] or "system"
    reason = str(reason or "").strip()[:300]
    scopes = (runtime_state.DEFAULT_CANCEL_SCOPE, PBS_CANCEL_SCOPE)
    previous_pause = _step(_previous_pause, database)
    state = {
        "active": True,
        "since": time.time(),
        "actor": actor,
        "reason": reason,
        "report": {},
        "previous_pause": previous_pause.get("result") or {"paused": False},
    }
    report: dict[str, Any] = {"state": _step(database.runtime_set, _KEY, state)}
    report["cancel"] = {scope: _step(rclone_sync.cancel_job, scope) for scope in scopes}
    # graceful_sec=0: SIGTERM, einmal prüfen, dann sofort SIGKILL.
    report["kill"] = {
        scope: _step(runtime_state.terminate_active_processes, 0, scope=scope)
        for scope in scopes
    }
    report["scheduler"] = _step(
        lambda: pause_scheduler(
            seconds=_PAUSE_SECONDS,
            reason=f"Lockdown: {reason}",
            actor=actor,
            db=database,
        )["until"]
    )
    try:
        report["remount"] = [
            _remount_ro(mountpoint, run) for mountpoint in _mountpoints()
        ]
    except Exception as exc:
        logger.exception("Mountpoints für den Lockdown nicht lesbar")
        report["remount"], report["remount_error"] = [], _error(exc)
    _fit(report)
    state["report"] = report
    report["report_saved"] = _step(database.runtime_set, _KEY, state)
    report["audit"] = _step(
        database.audit_add,
        "lockdown_activated",
        actor=actor,
        details={"reason": reason, "report": report},
    )
    # Synchron vor dem Session-Bump: der widerruft alle Geräte und leert die Outbox.
    # Zeitlimit je Gerät: notifications.apns.timeout_seconds (höchstens 30 s).
    push = _step(
        send_push_notifications,
        "lockdown",
        "Notfall-Lockdown aktiviert",
        f"Grund: {reason}"[:300],
        db=database,
    )
    counts = push.pop("result", None) or {}
    push["delivered"] = counts.get("sent", 0)
    # Wiederholungen löscht der Bump gleich mit, sie erreichen kein Gerät mehr.
    push["failed"] = counts.get("failed", 0) + counts.get("retrying", 0)
    report["notify"] = push
    report["sessions"] = _step(bump_session_version)
    report["sessions_invalidated"] = report["sessions"]["ok"]
    return report


def release_lockdown(*, actor: str, db: Database | None = None) -> dict[str, Any]:
    from .scheduler_control import pause_scheduler, resume_scheduler

    database = db or get_db()
    state = lockdown_state(database)
    if not state["active"]:
        return {"released": False, "manual_steps": []}
    remounted = [
        str(entry["mountpoint"])
        for entry in (state.get("report") or {}).get("remount") or []
        if isinstance(entry, dict) and entry.get("ok") is True
    ]
    database.runtime_delete(_KEY)
    previous = state.get("previous_pause") or {}
    restored_pause = False
    if previous.get("paused") and previous.get("until"):
        try:
            pause_scheduler(
                until=float(previous["until"]),
                reason=str(previous.get("reason") or ""),
                actor=actor,
                db=database,
            )
            restored_pause = True
        except ValueError:  # vorherige Pause ist inzwischen abgelaufen
            pass
    if not restored_pause:
        resume_scheduler(actor=actor, db=database)
    database.audit_add(
        "lockdown_released",
        actor=str(actor or "system")[:128],
        details={"since": state.get("since"), "remounted_ro": remounted},
    )
    return {
        "released": True,
        "manual_steps": [
            f"mount -o remount,rw {shlex.quote(mountpoint)}" for mountpoint in remounted
        ],
        "scheduler": {"restored_pause": restored_pause},
    }
