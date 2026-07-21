"""Proxmox-Backup-Server-Integration über proxmox-backup-client.

Eigener Job-Typ neben rclone: dateibasierte Backups konfigurierter Pfade in
einen PBS-Datastore (Dedup, Verschlüsselung und Retention übernimmt PBS).
Der Runner nutzt dieselbe Prozess-, Log- und Cancel-Infrastruktur wie rclone.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..config_store import get_config
from .rclone_sync import _run_rclone_command, _safe_name, is_cancelled, reset_cancel

logger = logging.getLogger(__name__)

JOB_KIND = "pbs"
PAIR_PREFIX = "pbs:"
_ARCHIVE_RE = re.compile(r"[^a-z0-9_-]+")
_KEEP_KEYS = ("keep-last", "keep-daily", "keep-weekly", "keep-monthly", "keep-yearly")


def client_path() -> Optional[str]:
    return shutil.which("proxmox-backup-client")


def pbs_settings(cfg=None) -> dict[str, Any]:
    cfg = cfg or get_config()
    section = cfg.get("pbs", default={}) or {}
    return section if isinstance(section, dict) else {}


def pbs_targets(settings: dict[str, Any]) -> list[dict[str, Any]]:
    targets = settings.get("targets") or []
    return [t for t in targets if isinstance(t, dict) and t.get("name")]


def _archive_name(path: str, used: set[str]) -> str:
    base = _ARCHIVE_RE.sub("-", Path(path).name.lower()).strip("-") or "root"
    candidate, counter = base, 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _subprocess_env(settings: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    password = str(settings.get("password") or "")
    if password:
        env["PBS_PASSWORD"] = password
    fingerprint = str(settings.get("fingerprint") or "").strip()
    if fingerprint:
        env["PBS_FINGERPRINT"] = fingerprint
    return env


def _base_args(settings: dict[str, Any], target: dict[str, Any]) -> list[str]:
    args = ["--repository", str(settings.get("repository") or "")]
    namespace = str(target.get("namespace") or settings.get("namespace") or "").strip()
    if namespace:
        args += ["--ns", namespace]
    return args


def build_backup_command(settings: dict[str, Any], target: dict[str, Any]) -> list[str]:
    client = client_path()
    if not client:
        raise RuntimeError("proxmox-backup-client ist nicht installiert")
    used: set[str] = set()
    archives = [
        f"{_archive_name(str(p), used)}.pxar:{p}"
        for p in (target.get("paths") or [])
        if str(p).strip()
    ]
    if not archives:
        raise RuntimeError("Target hat keine Pfade")
    cmd = [client, "backup", *archives, *_base_args(settings, target)]
    backup_id = str(
        target.get("backup_id") or settings.get("backup_id") or socket.gethostname()
    ).strip()
    if backup_id:
        cmd += ["--backup-id", backup_id]
    return cmd


def build_prune_command(
    settings: dict[str, Any], target: dict[str, Any]
) -> Optional[list[str]]:
    keep = settings.get("keep") or {}
    if not isinstance(keep, dict):
        return None
    keep_args: list[str] = []
    for key in _KEEP_KEYS:
        value = keep.get(key.replace("-", "_"), keep.get(key))
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            keep_args += [f"--{key}", str(count)]
    if not keep_args:
        return None
    client = client_path()
    if not client:
        return None
    backup_id = str(
        target.get("backup_id") or settings.get("backup_id") or socket.gethostname()
    ).strip()
    group = f"host/{backup_id}"
    return [client, "prune", group, *keep_args, *_base_args(settings, target)]


def _target_result(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": f"{PAIR_PREFIX}{name}", "kind": JOB_KIND, **extra}


def run_pbs_backup(
    targets_filter: Optional[list[str]] = None, *, trigger: str = "web"
) -> dict[str, Any]:
    """Führt PBS-Backups für alle (oder gefilterte) Targets aus.

    Rückgabe im selben Summary-Format wie run_job, damit db.job_finish die
    Läufe als pair_runs (mit Prefix "pbs:") persistiert und der Scheduler
    last_success/Retry darauf aufbauen kann.
    """
    cfg = get_config()
    settings = pbs_settings(cfg)
    summary: dict[str, Any] = {
        "ok": True,
        "kind": JOB_KIND,
        "trigger": trigger,
        "pairs": [],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }

    if not bool(settings.get("enabled", False)):
        summary.update(ok=False, error="PBS-Integration ist deaktiviert")
        return summary
    if not str(settings.get("repository") or "").strip():
        summary.update(ok=False, error="pbs.repository ist nicht konfiguriert")
        return summary
    if not client_path():
        summary.update(ok=False, error="proxmox-backup-client ist nicht installiert")
        return summary

    targets = pbs_targets(settings)
    if targets_filter:
        wanted = {str(t).removeprefix(PAIR_PREFIX) for t in targets_filter}
        targets = [t for t in targets if str(t.get("name")) in wanted]
    if not targets:
        summary.update(ok=False, error="Keine passenden PBS-Targets konfiguriert")
        return summary

    reset_cancel()
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "pbs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    timeout_sec = int(float(settings.get("timeout_hours", 4)) * 3600)
    extra_env = _subprocess_env(settings)

    for target in targets:
        name = str(target.get("name"))
        if is_cancelled():
            summary["pairs"].append(
                _target_result(name, ok=False, cancelled=True, skipped=True)
            )
            summary["ok"] = False
            continue
        started = time.time()
        log_file = (
            log_dir
            / f"pbs-{_safe_name(name, 'target')}-{datetime.now():%Y%m%d-%H%M%S}.log"
        )
        entry = _target_result(
            name,
            log_file=str(log_file),
            started_at=datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            trigger=trigger,
        )
        try:
            missing = [
                str(p)
                for p in (target.get("paths") or [])
                if str(p).strip() and not Path(str(p)).exists()
            ]
            if missing:
                raise RuntimeError(
                    f"Pfad(e) nicht vorhanden (Mount weg?): {', '.join(missing)}"
                )
            cmd = build_backup_command(settings, target)
            rc = _run_rclone_command(
                cmd,
                log_file,
                timeout_sec=timeout_sec,
                header=f"# PBS-Backup {name} um {datetime.now().isoformat()}\n"
                f"# {' '.join(cmd)}\n\n",
                pair_name=f"{PAIR_PREFIX}{name}",
                extra_env=extra_env,
            )
            entry["returncode"] = rc
            if rc == 130:
                entry.update(ok=False, cancelled=True)
            elif rc != 0:
                entry.update(ok=False, error=f"proxmox-backup-client Exit-Code {rc}")
            else:
                entry["ok"] = True
                prune_cmd = build_prune_command(settings, target)
                if prune_cmd and not is_cancelled():
                    prune_rc = _run_rclone_command(
                        prune_cmd,
                        log_file,
                        timeout_sec=min(timeout_sec, 1800),
                        append=True,
                        header=f"\n# Prune: {' '.join(prune_cmd)}\n\n",
                        pair_name=f"{PAIR_PREFIX}{name}",
                        extra_env=extra_env,
                    )
                    entry["prune_ok"] = prune_rc == 0
                    if prune_rc != 0:
                        entry["prune_error"] = f"Prune Exit-Code {prune_rc}"
        except Exception as exc:
            logger.exception("[%s] PBS-Backup fehlgeschlagen", name)
            entry.update(ok=False, error=str(exc))
        entry["duration_sec"] = round(time.time() - started, 1)
        if not entry.get("ok"):
            summary["ok"] = False
        summary["pairs"].append(entry)

    summary["ended_at"] = datetime.now().isoformat(timespec="seconds")
    _notify_result(summary)
    return summary


def _notify_result(summary: dict[str, Any]) -> None:
    try:
        from ..notifications import notify

        failed = [p["name"] for p in summary["pairs"] if not p.get("ok")]
        if summary.get("ok"):
            notify(
                "sync_ok",
                "PBS-Backup erfolgreich",
                f"{len(summary['pairs'])} Target(s) gesichert",
            )
        elif failed:
            notify(
                "sync_error",
                "PBS-Backup fehlgeschlagen",
                f"Fehler bei: {', '.join(failed)}",
            )
    except Exception:
        logger.exception("PBS-Benachrichtigung fehlgeschlagen")
