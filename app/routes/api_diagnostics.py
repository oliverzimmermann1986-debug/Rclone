"""Diagnose-/Doctor-Endpoint für Config, Scheduler, rclone und Mount-Schutz."""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from croniter import croniter
from fastapi import APIRouter, Depends

from .. import __version__
from ..auth import require_auth
from ..config_store import get_config
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..jobs.rclone_sync import _count_files_up_to, _is_remote, build_job_plan
from ..jobs.scheduler import DISABLED_VALUES, next_run_after, rclone_history_key
from ..rclone_args import rclone_subprocess_env
from ..scheduler_control import scheduler_state
from ..security import require_csrf
from ..system_info import system_snapshot

router = APIRouter(
    prefix="/api/diagnostics",
    tags=["diagnostics"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _item(name: str, level: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "ok": level != "error",
        "level": level,
        "message": message,
        **extra,
    }


def _ok(name: str, message: str, **extra: Any) -> dict[str, Any]:
    return _item(name, "ok", message, **extra)


def _warn(name: str, message: str, **extra: Any) -> dict[str, Any]:
    return _item(name, "warn", message, **extra)


def _err(name: str, message: str, **extra: Any) -> dict[str, Any]:
    return _item(name, "error", message, **extra)


def _rclone_version_check() -> dict[str, Any]:
    """Prüft die installierte rclone-Version und warnt bei zu alten Builds."""
    try:
        result = subprocess.run(
            ["rclone", "version"],
            capture_output=True,
            text=True,
            timeout=8,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        out = (result.stdout or "") + (result.stderr or "")
        match = re.search(r"rclone\s+v?(\d+\.\d+\.\d+)", out, re.IGNORECASE)
        ver = match.group(1) if match else "unknown"
        if ver == "unknown":
            return _warn(
                "rclone-version",
                "rclone Version konnte nicht geparst werden",
                version=ver,
            )

        parts = [int(x) for x in ver.split(".")]
        major = parts[0]
        minor = parts[1] if len(parts) > 1 else 0
        # Empfohlen ab 1.70 wegen Drive- und bisync-Verbesserungen
        if major > 1 or (major == 1 and minor >= 70):
            return _ok("rclone-version", f"rclone {ver}", version=ver)
        return _warn(
            "rclone-version",
            f"rclone {ver} ist relativ alt – empfohlen ≥ 1.70 "
            "(bessere Google-Drive- und bisync-Stabilität)",
            version=ver,
        )
    except FileNotFoundError:
        return _err("rclone-version", "rclone Binary nicht gefunden")
    except Exception as exc:
        return _err("rclone-version", f"rclone version fehlgeschlagen: {exc}")


def _writable_dir(path: str) -> dict[str, Any]:
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".doctor-write-test-{time.time_ns()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        usage = shutil.disk_usage(str(target))
        return _ok(
            str(target), "beschreibbar", free_bytes=usage.free, total_bytes=usage.total
        )
    except Exception as exc:
        return _err(str(target), f"nicht beschreibbar: {exc}")


_SYSTEMCTL_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_SYSTEMCTL_CACHE_LOCK = threading.Lock()
_SYSTEMCTL_CACHE_TTL = 10.0
_OVERVIEW_CACHE: tuple[float, dict[str, Any]] | None = None
_OVERVIEW_CACHE_LOCK = threading.Lock()
_OVERVIEW_BUILD_LOCK = threading.Lock()
_OVERVIEW_CACHE_TTL = 8.0


def _systemctl_state(unit: str) -> tuple[str, str]:
    now = time.monotonic()
    with _SYSTEMCTL_CACHE_LOCK:
        cached = _SYSTEMCTL_CACHE.get(unit)
        if cached and now - cached[0] < _SYSTEMCTL_CACHE_TTL:
            return cached[1]
    try:
        enabled = (
            subprocess.run(
                ["systemctl", "is-enabled", unit],
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            or "unknown"
        )
        active = (
            subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
            or "unknown"
        )
        result = (enabled, active)
    except (OSError, subprocess.TimeoutExpired):
        result = ("unknown", "unknown")
    with _SYSTEMCTL_CACHE_LOCK:
        _SYSTEMCTL_CACHE[unit] = (now, result)
    return result


@router.get("/doctor")
def doctor() -> dict[str, Any]:
    cfg = get_config()
    snapshot = cfg.snapshot()
    backup = snapshot.get("backup") or {}
    paths = snapshot.get("paths") or {}
    checks: list[dict[str, Any]] = []
    pair_checks: list[dict[str, Any]] = []

    try:
        _normalized, warnings = validate_config(snapshot)
        checks.append(_ok("Konfiguration", "Schema und Werte gültig"))
        checks.extend(_warn("Konfiguration", warning) for warning in warnings)
    except ConfigValidationError as exc:
        checks.extend(_err("Konfiguration", message) for message in exc.errors)

    try:
        db = get_db()
        integrity = db.integrity_check()
        stats = db.stats()
        if integrity.get("ok"):
            checks.append(
                _ok(
                    "SQLite",
                    "DB erreichbar und konsistent",
                    integrity=integrity,
                    stats=stats,
                )
            )
        else:
            checks.append(
                _err(
                    "SQLite",
                    "Integritätsprüfung fehlgeschlagen",
                    integrity=integrity,
                    stats=stats,
                )
            )
    except Exception as exc:
        checks.append(_err("SQLite", f"DB-Fehler: {exc}"))

    for label, path in {
        "data_dir": paths.get("data_dir", "/opt/rclone-sync/data"),
        "logs_dir": paths.get("logs_dir", "/opt/rclone-sync/logs"),
        "temp_dir": paths.get("temp_dir", "/opt/rclone-sync/temp"),
    }.items():
        result = _writable_dir(str(path))
        result["name"] = label
        checks.append(result)

    try:
        version = subprocess.run(
            ["rclone", "version"],
            capture_output=True,
            text=True,
            timeout=8,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if version.returncode == 0:
            checks.append(
                _ok(
                    "rclone",
                    version.stdout.splitlines()[0] if version.stdout else "rclone ok",
                )
            )
        else:
            checks.append(
                _err("rclone", (version.stderr or version.stdout).strip()[:300])
            )
    except FileNotFoundError:
        checks.append(_err("rclone", "Binary nicht gefunden"))
    except Exception as exc:
        checks.append(_err("rclone", f"Version-Check fehlgeschlagen: {exc}"))

    remotes: list[str] = []
    try:
        result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        remotes = (
            sorted(
                {
                    line.strip().rstrip(":")
                    for line in result.stdout.splitlines()
                    if line.strip()
                },
                key=str.casefold,
            )
            if result.returncode == 0
            else []
        )
        if remotes:
            checks.append(
                _ok(
                    "rclone remotes",
                    f"{len(remotes)} Remote(s): {', '.join(remotes)}",
                    remotes=remotes,
                )
            )
        else:
            checks.append(
                _warn(
                    "rclone remotes",
                    "keine Remotes gefunden",
                    stderr=result.stderr[:300],
                )
            )
    except Exception as exc:
        checks.append(_err("rclone remotes", f"listremotes fehlgeschlagen: {exc}"))

    filter_file = str(backup.get("filter_file") or "")
    if filter_file:
        checks.append(
            _ok("filter_file", f"gefunden: {filter_file}")
            if Path(filter_file).is_file()
            else _warn("filter_file", f"gesetzt, aber Datei fehlt: {filter_file}")
        )

    checks.append(
        _warn(
            "auto_resync",
            "aktiviert — automatische bisync-Reinitialisierung bewusst prüfen",
        )
        if backup.get("auto_resync")
        else _ok("auto_resync", "deaktiviert")
    )
    if backup.get("collect_pre_post_stats"):
        checks.append(
            _warn(
                "Pre/Post-Stats",
                "aktiviert — verursacht zusätzliche vollständige Traversierungen",
            )
        )
    else:
        checks.append(_ok("Pre/Post-Stats", "deaktiviert"))

    legacy_enabled, legacy_active = _systemctl_state("rclone-sync.timer")
    scheduler_enabled, scheduler_active = _systemctl_state("sync-scheduler.timer")
    if legacy_enabled == "enabled" and scheduler_enabled == "enabled":
        checks.append(
            _err(
                "Timer",
                "Legacy- und Per-Pair-Timer sind gleichzeitig aktiviert; Doppelstarts möglich",
            )
        )
    elif scheduler_enabled == "enabled":
        checks.append(
            _ok(
                "Timer",
                f"Per-Pair-Scheduler {scheduler_active}; Legacy-Timer {legacy_enabled}",
            )
        )
    else:
        checks.append(
            _warn(
                "Timer", f"Per-Pair-Scheduler: {scheduler_enabled}/{scheduler_active}"
            )
        )

    names: dict[str, int] = {}
    default_schedule = str(backup.get("default_schedule") or "").strip()
    timezone_name = str(backup.get("timezone") or "Europe/Berlin")
    now = time.time()
    for pair in backup.get("pairs") or []:
        name = str(pair.get("name") or "<ohne Name>")
        names[name] = names.get(name, 0) + 1
        pair_result: dict[str, Any] = {
            "name": name,
            "enabled": pair.get("enabled", True),
            "remote": pair.get("remote"),
            "local": pair.get("local"),
            "checks": [],
            "warnings": [],
        }
        remote_name = str(pair.get("remote") or "").split(":", 1)[0]
        if remote_name and remote_name not in remotes:
            pair_result["checks"].append(
                _err("remote", f"Remote '{remote_name}' ist nicht konfiguriert")
            )

        schedule = str(pair.get("schedule") or "").strip() or default_schedule
        if not schedule or schedule.lower() in DISABLED_VALUES:
            pair_result["schedule"] = {"enabled": False, "message": "manuell/off"}
        elif len(schedule.split()) == 5 and croniter.is_valid(schedule):
            pair_result["schedule"] = {
                "enabled": True,
                "expr": schedule,
                "timezone": timezone_name,
                "next_run": next_run_after(
                    schedule, after=now, timezone_name=timezone_name
                ),
            }
        else:
            pair_result["checks"].append(
                _err("schedule", f"Ungültige Cron-Expression: {schedule}")
            )

        local = str(pair.get("local") or "")
        if local and not _is_remote(local):
            local_path = Path(local)
            try:
                min_files = max(0, int(pair.get("min_local_files", 1) or 0))
            except (TypeError, ValueError):
                min_files = 1
            if not local_path.is_dir():
                pair_result["checks"].append(
                    _err("local", f"Pfad fehlt oder ist kein Verzeichnis: {local}")
                )
            elif min_files > 0:
                try:
                    count = _count_files_up_to(local_path, min_files)
                    pair_result["checks"].append(
                        _ok("mount_check", f">= {min_files} Dateien gefunden")
                        if count >= min_files
                        else _err(
                            "mount_check",
                            f"nur {count} Dateien, min_local_files={min_files}",
                        )
                    )
                except Exception as exc:
                    pair_result["checks"].append(
                        _warn("mount_check", f"nicht prüfbar: {exc}")
                    )
            else:
                pair_result["checks"].append(_warn("mount_check", "deaktiviert"))

        for file_key in ("include_file", "exclude_file", "filter_file"):
            configured_file = str(pair.get(file_key) or "").strip()
            if configured_file and not Path(configured_file).is_file():
                pair_result["checks"].append(
                    _err(file_key, f"Datei fehlt: {configured_file}")
                )

        if pair.get("require_mountpoint"):
            mountpoint = Path(str(pair.get("mountpoint") or local))
            if mountpoint.exists() and os.path.ismount(mountpoint):
                pair_result["checks"].append(
                    _ok("mountpoint", f"eingehängt: {mountpoint}")
                )
            else:
                pair_result["checks"].append(
                    _err("mountpoint", f"nicht eingehängt: {mountpoint}")
                )
        sentinel = str(pair.get("sentinel_file") or "").strip()
        if sentinel and local and not _is_remote(local):
            sentinel_path = Path(local) / sentinel
            pair_result["checks"].append(
                _ok("sentinel", f"gefunden: {sentinel}")
                if sentinel_path.is_file()
                else _err("sentinel", f"fehlt: {sentinel_path}")
            )

        direction = str(pair.get("direction") or "bisync").lower()
        mode = str(pair.get("mode") or "bisync").lower()
        destructive = direction == "bisync" or (
            direction in {"pull", "push"} and mode == "sync"
        )
        if destructive:
            if not pair.get("allow_delete"):
                pair_result["checks"].append(
                    _err("delete_confirmation", "allow_delete ist nicht aktiviert")
                )
            if pair.get("max_delete") in (None, "", -1, "-1"):
                pair_result["checks"].append(
                    _err("max_delete", "begrenztes max_delete fehlt")
                )
            pair_result["warnings"].append(
                "Mirror-Sync löscht im Ziel; Plan und Dry-Run vorher prüfen"
            )
        if pair.get("exclude") and pair.get("include"):
            pair_result["warnings"].append(
                "Include/Exclude kombiniert — Reihenfolge prüfen"
            )
        pair_checks.append(pair_result)

    duplicates = [name for name, count in names.items() if count > 1]
    checks.append(
        _err("Pair-Namen", "Doppelte Namen: " + ", ".join(duplicates))
        if duplicates
        else _ok("Pair-Namen", "eindeutig")
    )

    try:
        plan = build_job_plan(dry_run=True)
        checks.extend(_warn("Plan", warning) for warning in plan.get("warnings", []))
    except Exception as exc:
        checks.append(_err("Plan", f"Plan konnte nicht erstellt werden: {exc}"))

    checks.append(_rclone_version_check())

    all_items = checks + [
        check for pair in pair_checks for check in pair.get("checks", [])
    ]
    has_error = any(item.get("level") == "error" for item in all_items)
    has_warning = any(item.get("level") == "warn" for item in all_items) or any(
        pair.get("warnings") for pair in pair_checks
    )
    return {
        "ok": not has_error,
        "level": "error" if has_error else ("warn" if has_warning else "ok"),
        "checks": checks,
        "pairs": pair_checks,
        "generated_at": time.time(),
    }


def _cached_overview() -> dict[str, Any] | None:
    now_monotonic = time.monotonic()
    with _OVERVIEW_CACHE_LOCK:
        cached = _OVERVIEW_CACHE
        if cached and now_monotonic - cached[0] < _OVERVIEW_CACHE_TTL:
            return copy.deepcopy(cached[1])
    return None


@router.get("/overview")
def overview() -> dict[str, Any]:
    """Schnelle, gegen parallele Neuberechnung geschützte Betriebsübersicht."""
    cached = _cached_overview()
    if cached is not None:
        return cached

    # Nur ein Request baut den teuren Snapshot. Wartende Requests prüfen den
    # Cache nach Lock-Erwerb erneut und übernehmen das fertige Ergebnis.
    with _OVERVIEW_BUILD_LOCK:
        cached = _cached_overview()
        if cached is not None:
            return cached
        return _build_overview()


def _build_overview() -> dict[str, Any]:
    global _OVERVIEW_CACHE
    cfg = get_config().snapshot()
    backup = cfg.get("backup") or {}
    paths = cfg.get("paths") or {}
    pairs = [p for p in (backup.get("pairs") or []) if isinstance(p, dict)]
    enabled = [p for p in pairs if p.get("enabled", True)]
    default_schedule = str(backup.get("default_schedule") or "").strip()
    manual_values = {"", "manual", "off", "disabled", "none"}
    scheduled = []
    destructive = []
    for pair in enabled:
        schedule = str(pair.get("schedule") or "").strip() or default_schedule
        if schedule.casefold() not in manual_values:
            scheduled.append(pair)
        direction = str(pair.get("direction") or "bisync").casefold()
        mode = str(pair.get("mode") or "bisync").casefold()
        if direction == "bisync" or mode == "sync":
            destructive.append(pair)

    db = get_db()
    last_jobs = db.job_list(limit=20)
    last_job = last_jobs[0] if last_jobs else None
    last_success = next((job for job in last_jobs if job.get("status") == "ok"), None)
    last_error = next(
        (job for job in last_jobs if job.get("status") in {"error", "stale"}),
        None,
    )
    now = time.time()
    stats_24h = db.job_statistics(since=now - 86400)
    pair_health = []
    identities = {
        rclone_history_key(pair): str(pair.get("name") or "")
        for pair in enabled
        if str(pair.get("name") or "")
    }
    histories = db.pair_last_history(identities)
    for pair in enabled:
        name = str(pair.get("name") or "")
        history_key = rclone_history_key(pair)
        history = histories.get(history_key) or {}
        latest = history.get("last_result")
        latest_success = history.get("last_success")
        schedule = str(pair.get("schedule") or "").strip() or default_schedule
        next_run = None
        if (
            schedule.casefold() not in manual_values
            and len(schedule.split()) == 5
            and croniter.is_valid(schedule)
        ):
            try:
                next_run = next_run_after(
                    schedule,
                    after=now,
                    timezone_name=str(backup.get("timezone") or "Europe/Berlin"),
                )
            except Exception:
                next_run = None
        last_success_at = (
            float(latest_success.get("ended_at"))
            if latest_success and latest_success.get("ended_at")
            else None
        )
        max_success_age_hours = float(pair.get("max_success_age_hours") or 0)
        success_age_hours = (
            max(0.0, (now - last_success_at) / 3600.0)
            if last_success_at is not None
            else None
        )
        overdue = bool(
            max_success_age_hours > 0
            and (
                last_success_at is None
                or success_age_hours is None
                or success_age_hours > max_success_age_hours
            )
        )
        pair_health.append(
            {
                "name": name,
                "history_key": history_key,
                "direction": pair.get("direction", "bisync"),
                "mode": pair.get("mode", "bisync"),
                "schedule": schedule,
                "next_run": next_run,
                "last_status": latest.get("status") if latest else None,
                "last_run": latest.get("ended_at") if latest else None,
                "job_id": latest.get("job_id") if latest else None,
                "last_success": last_success_at,
                "success_age_hours": round(success_age_hours, 1)
                if success_age_hours is not None
                else None,
                "max_success_age_hours": max_success_age_hours,
                "overdue": overdue,
                "error": ((latest.get("pair") or {}).get("error") if latest else None),
            }
        )

    scheduler_enabled, scheduler_active = _systemctl_state("sync-scheduler.timer")
    scheduler_control = scheduler_state(db, now=now)
    web_enabled, web_active = _systemctl_state("rclone-sync-web.service")
    system = system_snapshot(str(paths.get("data_dir") or "/opt/rclone-sync/data"))
    alerts: list[dict[str, str]] = []
    if not bool(backup.get("enabled", True)):
        alerts.append(
            {
                "level": "info",
                "message": "Automatische Zeitpläne sind in der Konfiguration deaktiviert",
            }
        )
    elif scheduler_control.get("paused"):
        until = scheduler_control.get("until")
        alerts.append(
            {
                "level": "info",
                "message": "Scheduler ist für ein Wartungsfenster pausiert"
                + (
                    f" bis {datetime.fromtimestamp(float(until)).strftime('%d.%m. %H:%M')}"
                    if until
                    else ""
                ),
            }
        )
    elif scheduler_active != "active":
        alerts.append(
            {"level": "warn", "message": "Per-Pair-Scheduler ist nicht aktiv"}
        )
    if last_job and last_job.get("status") in {"error", "stale"}:
        alerts.append(
            {"level": "error", "message": "Der letzte Job ist fehlgeschlagen"}
        )
    if destructive and any(not pair.get("allow_delete") for pair in destructive):
        alerts.append(
            {
                "level": "info",
                "message": "Mindestens ein Mirror-/Bi-Sync-Pair ist noch nicht für Löschungen freigegeben",
            }
        )
    overdue_pairs = [item for item in pair_health if item.get("overdue")]
    if overdue_pairs:
        names = ", ".join(str(item.get("name")) for item in overdue_pairs[:3])
        suffix = " …" if len(overdue_pairs) > 3 else ""
        alerts.append(
            {
                "level": "warn",
                "message": f"{len(overdue_pairs)} Pair(s) ohne frischen erfolgreichen Lauf: {names}{suffix}",
            }
        )

    if system.get("memory", {}).get("percent_used", 0) >= 90:
        alerts.append(
            {
                "level": "warn",
                "message": "Arbeitsspeicher ist zu mindestens 90 % belegt",
            }
        )
    if system.get("data_disk", {}).get("percent_used", 0) >= 90:
        alerts.append(
            {
                "level": "error",
                "message": "Datenträger für Anwendungsdaten ist fast voll",
            }
        )
    pids_percent = system.get("pids", {}).get("percent_used")
    if pids_percent is not None and pids_percent >= 90:
        alerts.append(
            {
                "level": "warn",
                "message": "Prozesslimit des Proxmox-Gasts ist zu mindestens 90 % belegt",
            }
        )

    result = {
        "app": {
            "version": __version__,
            "timezone": backup.get("timezone", "Europe/Berlin"),
        },
        "system": system,
        "services": {
            "web": {"enabled": web_enabled, "active": web_active},
            "scheduler": {
                "enabled": scheduler_enabled,
                "active": scheduler_active,
                "configured_enabled": bool(backup.get("enabled", True)),
                "control": scheduler_control,
            },
        },
        "pairs": {
            "total": len(pairs),
            "enabled": len(enabled),
            "scheduled": len(scheduled),
            "manual": len(enabled) - len(scheduled),
            "destructive": len(destructive),
            "health": pair_health,
        },
        "jobs": {
            "last": last_job,
            "last_success": last_success,
            "last_error": last_error,
            "stats_24h": stats_24h,
        },
        "alerts": alerts,
        "generated_at": now,
    }
    with _OVERVIEW_CACHE_LOCK:
        _OVERVIEW_CACHE = (time.monotonic(), copy.deepcopy(result))
    return result
