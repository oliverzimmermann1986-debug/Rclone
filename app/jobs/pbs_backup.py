"""Proxmox-Backup-Server-Integration über proxmox-backup-client.

Eigener Job-Typ neben rclone: dateibasierte Backups konfigurierter Pfade in
einen PBS-Datastore (Dedup, Verschlüsselung und Retention übernimmt PBS).
Der Runner nutzt dieselbe Prozess-, Log- und Cancel-Infrastruktur wie rclone.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from ..config_store import get_config
from .rclone_sync import (
    _SnapshotConfig,
    _run_rclone_command,
    _safe_name,
    command_to_string,
    is_cancelled,
    reset_cancel,
)
from .scheduler import pbs_history_key

logger = logging.getLogger(__name__)

JOB_KIND = "pbs"
PAIR_PREFIX = "pbs:"
PBS_CANCEL_SCOPE = "pbs"
_ARCHIVE_RE = re.compile(r"[^a-z0-9_-]+")
_KEEP_KEYS = ("keep-last", "keep-daily", "keep-weekly", "keep-monthly", "keep-yearly")
_NOTIFY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pbs-notify")
_NOTIFY_SLOTS = threading.BoundedSemaphore(value=4)


def client_path() -> Optional[str]:
    return shutil.which("proxmox-backup-client")


def pbs_settings(cfg=None) -> dict[str, Any]:
    cfg = cfg or get_config()
    if isinstance(cfg, Mapping):
        section = cfg.get("pbs") or {}
    else:
        section = cfg.get("pbs", default={}) or {}
    return dict(section) if isinstance(section, Mapping) else {}


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


def _target_backup_id(settings: dict[str, Any], target: dict[str, Any]) -> str:
    backup_id = str(
        target.get("backup_id") or settings.get("backup_id") or socket.gethostname()
    ).strip()
    # Die Backup-ID ist die PBS-Snapshot-Gruppe. Sie darf nicht implizit aus dem
    # Anzeigenamen des Targets erweitert werden, da das bestehende Snapshot-
    # Historien und Retention-Gruppen verwaisen würde. Mehrere Targets benötigen
    # deshalb bereits bei der Config-Validierung explizite, eindeutige IDs.
    return _safe_name(backup_id, "host")


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
    backup_id = _target_backup_id(settings, target)
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
    backup_id = _target_backup_id(settings, target)
    group = f"host/{backup_id}"
    return [client, "prune", group, *keep_args, *_base_args(settings, target)]


def _target_result(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": f"{PAIR_PREFIX}{name}", "kind": JOB_KIND, **extra}


def _count_files_up_to(path: Path, limit: int) -> int:
    if limit <= 0:
        return 0
    count = 0
    for _root, _dirs, files in os.walk(path):
        if is_cancelled(PBS_CANCEL_SCOPE):
            raise RuntimeError("PBS-Backup wurde während des Source-Checks abgebrochen")
        count += len(files)
        if count >= limit:
            return count
    return count


def _target_mountpoint(target: dict[str, Any], source: Path) -> Path:
    configured = str(target.get("mountpoint") or "").strip()
    return Path(configured) if configured else source


def _check_target_sources(
    target: dict[str, Any], *, include_counts: bool
) -> tuple[bool, str]:
    paths = [Path(str(value)) for value in (target.get("paths") or []) if str(value)]
    require_mountpoint = bool(target.get("require_mountpoint", False))
    sentinel = str(target.get("sentinel_file") or "").strip()
    try:
        min_files = max(0, int(target.get("min_files", 1)))
    except (TypeError, ValueError):
        min_files = 1

    for source in paths:
        if not source.exists() or not source.is_dir():
            return False, f"Pfad nicht vorhanden oder kein Verzeichnis: {source}"
        if require_mountpoint:
            mountpoint = _target_mountpoint(target, source)
            if not mountpoint.exists() or not os.path.ismount(mountpoint):
                return (
                    False,
                    f"Erwarteter PBS-Mountpoint ist nicht eingehängt: {mountpoint}",
                )
            try:
                source.resolve().relative_to(mountpoint.resolve())
            except (OSError, RuntimeError, ValueError):
                return False, f"PBS-Pfad liegt nicht unter Mountpoint: {source}"
        if sentinel and not (source / sentinel).is_file():
            return False, f"PBS-Sentinel-Datei fehlt: {source / sentinel}"
        if include_counts and min_files > 0:
            count = _count_files_up_to(source, min_files)
            if count < min_files:
                return False, (
                    f"Nur {count} Dateien unter {source}, min_files={min_files}; "
                    "Mount-Drop vermutet."
                )
    return True, "ok"


def _source_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    return {
        "resolved": str(resolved),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def _capture_target_guards(target: dict[str, Any]) -> dict[str, dict[str, Any]]:
    guards: dict[str, dict[str, Any]] = {}
    require_mountpoint = bool(target.get("require_mountpoint", False))
    sentinel = str(target.get("sentinel_file") or "").strip()
    for value in target.get("paths") or []:
        source = Path(str(value))
        guard: dict[str, Any] = {"identity": _source_identity(source)}
        if require_mountpoint:
            guard["mountpoint_identity"] = _source_identity(
                _target_mountpoint(target, source)
            )
        if sentinel:
            guard["sentinel_identity"] = _source_identity(source / sentinel)
        guards[str(source)] = guard
    return guards


def _recheck_target_guards(
    target: dict[str, Any], guards: dict[str, dict[str, Any]]
) -> tuple[bool, str]:
    ok, message = _check_target_sources(target, include_counts=False)
    if not ok:
        return False, message
    require_mountpoint = bool(target.get("require_mountpoint", False))
    sentinel = str(target.get("sentinel_file") or "").strip()
    for value in target.get("paths") or []:
        source = Path(str(value))
        guard = guards.get(str(source))
        if guard is None:
            return False, f"Kein Identitäts-Snapshot für PBS-Pfad: {source}"
        try:
            if _source_identity(source) != guard["identity"]:
                return False, f"PBS-Pfadidentität hat sich geändert: {source}"
            if require_mountpoint and _source_identity(
                _target_mountpoint(target, source)
            ) != guard.get("mountpoint_identity"):
                return False, f"PBS-Mountpoint-Identität hat sich geändert: {source}"
            if sentinel and _source_identity(source / sentinel) != guard.get(
                "sentinel_identity"
            ):
                return False, f"PBS-Sentinel-Identität hat sich geändert: {source}"
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            return False, f"PBS-Pfadidentität konnte nicht bestätigt werden: {exc}"
    return True, "ok"


def run_pbs_backup(
    targets_filter: Optional[list[str]] = None,
    *,
    trigger: str = "web",
    reset_cancel_state: bool = True,
    config_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Führt PBS-Backups für alle (oder gefilterte) Targets aus.

    Rückgabe im selben Summary-Format wie run_job, damit db.job_finish die
    Läufe als pair_runs (mit Prefix "pbs:") persistiert und der Scheduler
    last_success/Retry darauf aufbauen kann.
    """
    cfg = (
        _SnapshotConfig(dict(config_snapshot))
        if config_snapshot is not None
        else get_config()
    )
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

    if reset_cancel_state:
        reset_cancel(PBS_CANCEL_SCOPE)
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "pbs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    timeout_sec = int(float(settings.get("timeout_hours", 4)) * 3600)
    extra_env = _subprocess_env(settings)

    for target in targets:
        name = str(target.get("name"))
        if is_cancelled(PBS_CANCEL_SCOPE):
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
            history_key=pbs_history_key(settings, target),
            log_file=str(log_file),
            started_at=datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            trigger=trigger,
        )
        try:
            sources_ok, sources_message = _check_target_sources(
                target, include_counts=True
            )
            if not sources_ok:
                raise RuntimeError(sources_message)
            source_guards = _capture_target_guards(target)
            cmd = build_backup_command(settings, target)
            target_run_id = (
                f"pbs:{_target_backup_id(settings, target)}:{int(started * 1000)}"
            )
            rc = _run_rclone_command(
                cmd,
                log_file,
                timeout_sec=timeout_sec,
                max_runtime_sec=timeout_sec,
                header=f"# PBS-Backup {name} um {datetime.now().isoformat()}\n"
                f"# {command_to_string(cmd)}\n\n",
                pair_name=f"{PAIR_PREFIX}{name}",
                extra_env=extra_env,
                cancel_scope=PBS_CANCEL_SCOPE,
                run_id=target_run_id,
                pre_spawn_check=lambda _target=target, _guards=source_guards: (
                    _recheck_target_guards(_target, _guards)
                ),
            )
            entry["returncode"] = rc
            if rc == 130:
                entry.update(ok=False, cancelled=True)
            elif rc != 0:
                entry.update(ok=False, error=f"proxmox-backup-client Exit-Code {rc}")
            else:
                entry["ok"] = True
                prune_cmd = build_prune_command(settings, target)
                if is_cancelled(PBS_CANCEL_SCOPE):
                    entry.update(ok=False, cancelled=True, error="Abgebrochen")
                elif prune_cmd:
                    prune_rc = _run_rclone_command(
                        prune_cmd,
                        log_file,
                        timeout_sec=min(timeout_sec, 1800),
                        max_runtime_sec=min(timeout_sec, 1800),
                        append=True,
                        header=f"\n# Prune: {command_to_string(prune_cmd)}\n\n",
                        pair_name=f"{PAIR_PREFIX}{name}",
                        extra_env=extra_env,
                        cancel_scope=PBS_CANCEL_SCOPE,
                        run_id=target_run_id,
                    )
                    entry["prune_ok"] = prune_rc == 0
                    if prune_rc == 130 and is_cancelled(PBS_CANCEL_SCOPE):
                        entry.update(ok=False, cancelled=True, error="Abgebrochen")
                    elif prune_rc != 0:
                        prune_error = f"Prune Exit-Code {prune_rc}"
                        entry.update(
                            ok=False,
                            degraded=True,
                            prune_error=prune_error,
                            error=prune_error,
                        )
        except Exception as exc:
            logger.exception("[%s] PBS-Backup fehlgeschlagen", name)
            entry.update(ok=False, error=str(exc))
            if is_cancelled(PBS_CANCEL_SCOPE):
                entry.update(cancelled=True, error="Abgebrochen")
        entry["duration_sec"] = round(time.time() - started, 1)
        if not entry.get("ok"):
            summary["ok"] = False
        summary["pairs"].append(entry)

    summary["cancelled"] = any(
        bool(entry.get("cancelled")) for entry in summary["pairs"]
    )
    summary["ended_at"] = datetime.now().isoformat(timespec="seconds")
    _notify_result(summary)
    return summary


def _notify_result_sync(summary: dict[str, Any]) -> None:
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


def _notify_result_worker(summary: dict[str, Any]) -> None:
    try:
        _notify_result_sync(summary)
    finally:
        _NOTIFY_SLOTS.release()


def _notify_result(summary: dict[str, Any]) -> None:
    """Web-Läufe nicht auf externe Notification-I/O warten lassen."""
    if summary.get("trigger") != "web":
        _notify_result_sync(summary)
        return
    if not _NOTIFY_SLOTS.acquire(blocking=False):
        logger.warning("PBS-Benachrichtigung verworfen: Warteschlange ist voll")
        return
    try:
        _NOTIFY_EXECUTOR.submit(_notify_result_worker, dict(summary))
    except RuntimeError:
        _NOTIFY_SLOTS.release()
        logger.exception("PBS-Benachrichtigung konnte nicht eingeplant werden")
