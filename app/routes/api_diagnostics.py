"""Diagnose-/Doctor-Endpoint für Config, Scheduler, rclone und Mount-Schutz."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from croniter import croniter
from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.rclone_sync import _count_files_up_to, _is_remote, build_job_plan
from ..jobs.scheduler import DISABLED_VALUES, next_run_after

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"], dependencies=[Depends(require_auth)])


def _ok(name: str, message: str, **extra) -> Dict[str, Any]:
    return {"name": name, "ok": True, "level": "ok", "message": message, **extra}


def _warn(name: str, message: str, **extra) -> Dict[str, Any]:
    return {"name": name, "ok": True, "level": "warn", "message": message, **extra}


def _err(name: str, message: str, **extra) -> Dict[str, Any]:
    return {"name": name, "ok": False, "level": "error", "message": message, **extra}


def _writable_dir(path: str) -> Dict[str, Any]:
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        usage = shutil.disk_usage(str(p))
        return _ok(str(p), "beschreibbar", free_bytes=usage.free, total_bytes=usage.total)
    except Exception as e:
        return _err(str(p), f"nicht beschreibbar: {e}")


@router.get("/doctor")
def doctor() -> Dict[str, Any]:
    cfg = get_config()
    db = get_db()
    backup = cfg.get("backup", default={}) or {}
    paths = cfg.get("paths", default={}) or {}
    checks: List[Dict[str, Any]] = []
    pair_checks: List[Dict[str, Any]] = []

    # Core checks
    try:
        with db.conn() as c:
            c.execute("SELECT 1").fetchone()
        checks.append(_ok("SQLite", "DB erreichbar"))
    except Exception as e:
        checks.append(_err("SQLite", f"DB Fehler: {e}"))

    for label, path in {
        "data_dir": paths.get("data_dir", "/opt/rclone-sync/data"),
        "logs_dir": paths.get("logs_dir", "/opt/rclone-sync/logs"),
        "temp_dir": paths.get("temp_dir", "/opt/rclone-sync/temp"),
    }.items():
        c = _writable_dir(path)
        c["name"] = label
        checks.append(c)

    try:
        rv = subprocess.run(["rclone", "version"], capture_output=True, text=True, timeout=8)
        if rv.returncode == 0:
            checks.append(_ok("rclone", rv.stdout.splitlines()[0] if rv.stdout else "rclone ok"))
        else:
            checks.append(_err("rclone", (rv.stderr or rv.stdout).strip()[:300]))
    except FileNotFoundError:
        checks.append(_err("rclone", "Binary nicht gefunden"))
    except Exception as e:
        checks.append(_err("rclone", f"Version-Check fehlgeschlagen: {e}"))

    try:
        rr = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
        remotes = [ln.strip().rstrip(":") for ln in rr.stdout.splitlines() if ln.strip()] if rr.returncode == 0 else []
        if remotes:
            checks.append(_ok("rclone remotes", f"{len(remotes)} Remote(s): {', '.join(remotes)}", remotes=remotes))
        else:
            checks.append(_warn("rclone remotes", "keine Remotes gefunden oder listremotes leer", stderr=rr.stderr[:300]))
    except Exception as e:
        checks.append(_err("rclone remotes", f"listremotes fehlgeschlagen: {e}"))

    filter_file = backup.get("filter_file") or ""
    if filter_file:
        if Path(filter_file).exists():
            checks.append(_ok("filter_file", f"gefunden: {filter_file}"))
        else:
            checks.append(_warn("filter_file", f"gesetzt, aber Datei fehlt: {filter_file}"))

    if backup.get("auto_resync"):
        checks.append(_warn("auto_resync", "aktiviert — bei bisync nur bewusst verwenden"))
    else:
        checks.append(_ok("auto_resync", "deaktiviert"))

    pairs = backup.get("pairs") or []
    names: Dict[str, int] = {}
    default_schedule = (backup.get("default_schedule") or "").strip()
    now = time.time()
    for p in pairs:
        name = p.get("name") or "<ohne Name>"
        names[name] = names.get(name, 0) + 1
        pc: Dict[str, Any] = {
            "name": name,
            "enabled": p.get("enabled", True),
            "remote": p.get("remote"),
            "local": p.get("local"),
            "checks": [],
            "warnings": [],
        }
        if not p.get("name"):
            pc["checks"].append(_err("name", "Name fehlt"))
        if not p.get("remote"):
            pc["checks"].append(_err("remote", "Remote fehlt"))
        if not p.get("local"):
            pc["checks"].append(_err("local", "Lokaler Pfad fehlt"))

        schedule = (p.get("schedule") or "").strip() or default_schedule
        if not schedule or schedule.lower() in DISABLED_VALUES:
            pc["schedule"] = {"enabled": False, "message": "manuell/off"}
        elif croniter.is_valid(schedule):
            pc["schedule"] = {"enabled": True, "expr": schedule, "next_run": next_run_after(schedule, after=now)}
        else:
            pc["checks"].append(_err("schedule", f"Ungültige Cron Expression: {schedule}"))

        local = str(p.get("local") or "")
        if local and not _is_remote(local):
            lp = Path(local)
            min_files = int(p.get("min_local_files", 1) or 0)
            if not lp.exists():
                pc["checks"].append(_err("local", f"Pfad existiert nicht: {local}"))
            else:
                try:
                    count = _count_files_up_to(lp, max(1, min_files)) if min_files > 0 else None
                    if min_files > 0 and (count or 0) < min_files:
                        pc["checks"].append(_err("mount_check", f"nur {count} Dateien, min_local_files={min_files}"))
                    elif min_files > 0:
                        pc["checks"].append(_ok("mount_check", f">= {min_files} Dateien gefunden"))
                    else:
                        pc["checks"].append(_warn("mount_check", "deaktiviert"))
                except Exception as e:
                    pc["checks"].append(_warn("mount_check", f"nicht prüfbar: {e}"))

        direction = (p.get("direction") or "bisync").lower()
        mode = (p.get("mode") or "bisync").lower()
        if direction in ("pull", "push") and mode == "sync":
            pc["warnings"].append("Mirror-Sync löscht im Ziel; max_delete und Dry-Run empfohlen")
        if p.get("exclude") and p.get("include"):
            pc["warnings"].append("Include/Exclude kombiniert — Reihenfolge prüfen")
        pair_checks.append(pc)

    dupes = [n for n, c in names.items() if c > 1]
    if dupes:
        checks.append(_err("Pair-Namen", "Doppelte Namen: " + ", ".join(dupes)))
    else:
        checks.append(_ok("Pair-Namen", "eindeutig"))

    plan = build_job_plan(dry_run=True)
    for w in plan.get("warnings", []):
        checks.append(_warn("Plan", w))

    ok = all(c.get("ok", False) for c in checks) and all(
        all(ch.get("ok", False) for ch in pc.get("checks", [])) for pc in pair_checks
    )
    level = "ok" if ok else ("error" if any(c.get("level") == "error" for c in checks) else "warn")
    return {"ok": ok, "level": level, "checks": checks, "pairs": pair_checks, "generated_at": time.time()}
