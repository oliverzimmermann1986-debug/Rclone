"""Boot-Drill: jüngstes VM-Disk-Image einer PBS-Gruppe headless booten.

Das Image wird als Raw-Datei in einen flüchtigen Arbeitsbereich (Modus 0700
unter scratch_dir) zurückgeholt und mit QEMU ohne Netzwerk (-nodefaults)
gestartet. Bestanden ist der Drill, sobald der QEMU Guest Agent auf
guest-ping antwortet. Das Image enthält Produktivdaten; der Arbeitsbereich
wird darum immer gelöscht, auch bei Fehlern.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..config_store import get_config
from ..lockdown import is_locked_down
from ..notifications import notify
from ..utils import bounded_int
from . import runtime_state
from .pbs_backup import (
    PBS_CANCEL_SCOPE,
    _base_args,
    _subprocess_env,
    client_path,
    pbs_settings,
)
from .rclone_sync import (
    _run_rclone_command,
    _SnapshotConfig,
    _terminate_proc,
    command_to_string,
)
from .scheduler import (
    DEFAULT_TIMEZONE,
    _evaluate_due,
    _is_disabled,
    _is_valid_schedule,
    _load_history,
    next_run_after,
)

# Fester Name statt __name__: JobScopeFilter("bootdrill") filtert auf app.jobs.bootdrill.
logger = logging.getLogger("app.jobs.bootdrill")

JOB_KIND = "bootdrill"
RUN_NAME = "boot-drill"
BOOT_DRILL_HISTORY_KEY = "bootdrill:global"
QEMU_BINARY = "qemu-system-x86_64"
# Schutz vor Options-Injection in die argv von proxmox-backup-client.
_GROUP_RE = re.compile(r"^[a-z]+/[A-Za-z0-9_][A-Za-z0-9._-]*$")
_LIST_TIMEOUT_SEC = 300
_POLL_SEC = 2
_SERIAL_TAIL_BYTES = 2048
_FREE_SPACE_MARGIN = 512 * 1024 * 1024


def boot_drill_settings(cfg) -> dict[str, Any]:
    raw = pbs_settings(cfg).get("boot_drill")
    if not isinstance(raw, Mapping):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "schedule": str(raw.get("schedule") or "manual").strip(),
        "group": str(raw.get("group") or "").strip(),
        "archive": str(raw.get("archive") or "").strip(),
        "memory_mb": bounded_int(
            raw.get("memory_mb", 1024), default=1024, minimum=256, maximum=65536
        ),
        "timeout_seconds": bounded_int(
            raw.get("timeout_seconds", 60), default=60, minimum=10, maximum=900
        ),
        "scratch_dir": str(raw.get("scratch_dir") or "").strip(),
    }


def _unix_connect(path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_POLL_SEC)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        raise
    return sock


def _guest_ping(connect: Callable[[str], Any], path: str) -> bool:
    try:
        with connect(path) as sock:
            sock.sendall(b'{"execute":"guest-ping"}\n')
            return b'"return"' in sock.recv(4096)
    except OSError:
        return False


def _serial_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _SERIAL_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _drill(cfg, summary: dict[str, Any], run, popen, connect) -> None:
    settings = pbs_settings(cfg)
    drill = boot_drill_settings(cfg)
    if not bool(settings.get("enabled", False)):
        raise RuntimeError("PBS-Integration ist deaktiviert")
    if not str(settings.get("repository") or "").strip():
        raise RuntimeError("pbs.repository ist nicht konfiguriert")
    group = drill["group"]
    if not _GROUP_RE.match(group):
        raise RuntimeError(
            "pbs.boot_drill.group fehlt oder ist ungültig (Beispiel: vm/100)"
        )
    client = client_path()
    if not client:
        raise RuntimeError("proxmox-backup-client nicht installiert")
    qemu = shutil.which(QEMU_BINARY)
    if not qemu:
        raise RuntimeError(f"{QEMU_BINARY} nicht installiert")

    env = {**os.environ, **_subprocess_env(settings)}
    base = _base_args(settings, {})
    listing = run(
        [client, "snapshot", "list", group, "--output-format", "json", *base],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=_LIST_TIMEOUT_SEC,
    )
    if listing.returncode != 0:
        detail = str(listing.stderr or "").strip()[-300:]
        raise RuntimeError(
            f"Snapshot-Liste fehlgeschlagen (Exit-Code {listing.returncode}): {detail}"
        )
    snapshots = [s for s in json.loads(listing.stdout or "[]") if isinstance(s, dict)]
    if not snapshots:
        raise RuntimeError(f"Keine Snapshots in Gruppe {group}")
    latest = max(snapshots, key=lambda s: int(s.get("backup-time") or 0))
    stamp = datetime.fromtimestamp(int(latest["backup-time"]), tz=timezone.utc)
    snapshot = (
        f"{latest['backup-type']}/{latest['backup-id']}/"
        f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    summary["snapshot"] = snapshot

    wanted = drill["archive"]
    images = [
        f
        for f in latest.get("files") or []
        if isinstance(f, Mapping) and str(f.get("filename") or "").endswith(".img.fidx")
    ]
    entry = next((f for f in images if not wanted or f["filename"] == wanted), None)
    if entry is None:
        raise RuntimeError(
            f"Archiv {wanted} ist nicht im Snapshot {snapshot} enthalten"
            if wanted
            else f"Snapshot {snapshot} enthält kein Disk-Image (*.img.fidx)"
        )
    archive = str(entry["filename"])
    summary["archive"] = archive

    scratch_root = Path(
        drill["scratch_dir"]
        or cfg.get("paths", "temp_dir", default="/opt/rclone-sync/temp")
    )
    scratch_root.mkdir(parents=True, exist_ok=True)
    # Reste hart beendeter Läufe; sicher, weil der Drill exklusiv im PBS-Scope läuft.
    stale = [
        p
        for p in scratch_root.glob("sicherpfad-bootdrill-*")
        if p.is_dir() and not p.is_symlink()
    ]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            logger.error("Alter Boot-Drill-Arbeitsbereich %s blieb bestehen", path)
    summary["stale_cleaned"] = sum(not p.exists() for p in stale)
    size = int(entry.get("size") or 0)
    free = shutil.disk_usage(scratch_root).free
    if size and free < size + _FREE_SPACE_MARGIN:
        raise RuntimeError(
            f"Nicht genügend Platz unter {scratch_root}: "
            f"{size} Byte benötigt, {free} Byte frei"
        )
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "pbs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bootdrill-{datetime.now():%Y%m%d-%H%M%S}.log"
    summary["log_file"] = str(log_file)

    # mkdtemp legt das Verzeichnis mit Modus 0700 an.
    scratch = Path(
        tempfile.mkdtemp(prefix="sicherpfad-bootdrill-", dir=str(scratch_root))
    )
    log = None
    proc = None
    try:
        disk = scratch / "disk.raw"
        restore_cmd = [client, "restore", snapshot, archive, str(disk), *base]
        timeout = int(float(settings.get("timeout_hours", 4)) * 3600)
        # Wie pbs_backup im PBS-Scope registriert: Lockdown/Abbruch beenden den Restore.
        rc = _run_rclone_command(
            restore_cmd,
            log_file,
            timeout_sec=timeout,
            max_runtime_sec=timeout,
            header=f"# Boot-Drill {snapshot} {archive}\n"
            f"# {command_to_string(restore_cmd)}\n",
            pair_name=RUN_NAME,
            extra_env=_subprocess_env(settings),
            cancel_scope=PBS_CANCEL_SCOPE,
            run_id=f"{JOB_KIND}:{int(time.time() * 1000)}",
        )
        if rc == 130:
            raise RuntimeError("abgebrochen")
        if rc != 0:
            raise RuntimeError(
                f"Zurückholen fehlgeschlagen (proxmox-backup-client Exit-Code {rc})"
            )
        if is_locked_down():
            raise RuntimeError("Lockdown aktiv")

        log = open(log_file, "ab", opener=lambda p, f: os.open(p, f, 0o600))
        kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
        summary["kvm"] = kvm
        sock_path = str(scratch / "qga.sock")
        cmd = [
            qemu,
            "-m", str(drill["memory_mb"]),
            "-display", "none",
            "-nodefaults",
            "-no-user-config",
            "-snapshot",
            "-drive", f"file={disk},format=raw,if=virtio",
            "-chardev", f"socket,id=qga0,path={sock_path},server=on,wait=off",
            "-device", "virtio-serial",
            "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-serial", f"file:{scratch / 'serial.log'}",
            "-monitor", "none",
        ]  # fmt: skip
        if kvm:
            cmd.append("-enable-kvm")
        log.write(f"\n# {command_to_string(cmd)}\n".encode())
        log.flush()
        proc = popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            # Overlay von -snapshot landet so ebenfalls im Arbeitsbereich.
            env={**os.environ, "TMPDIR": str(scratch)},
        )
        # Registriert, damit Lockdown/Abbruch QEMU über den PBS-Scope beenden.
        runtime_state.register_process(
            proc.pid, log_file=str(log_file), scope=PBS_CANCEL_SCOPE, executable=qemu
        )
        started = time.monotonic()
        deadline = started + drill["timeout_seconds"]
        while not _guest_ping(connect, sock_path):
            rc = proc.poll()
            if rc is not None:
                hint = (
                    ""
                    if kvm
                    else "; kein Zugriff auf /dev/kvm – unter MemoryDenyWriteExecute=true "
                    "kann QEMU nicht starten, Details im Joblog"
                )
                raise RuntimeError(f"QEMU vorzeitig beendet (Exit-Code {rc}){hint}")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Kein Lebenszeichen vom QEMU Guest Agent nach "
                    f"{drill['timeout_seconds']} s – Gast bootet nicht oder "
                    "qemu-guest-agent ist nicht installiert"
                )
            time.sleep(_POLL_SEC)
        summary.update(
            bootable=True, seconds_to_agent=round(time.monotonic() - started, 1)
        )
    finally:
        cleanup: list[str] = []
        if proc is not None:
            try:
                _terminate_proc(proc, graceful_sec=5)
                # Erst nach bestätigtem Exit, sonst bleibt der Marker als Warnsignal.
                runtime_state.unregister_process(proc.pid)
            except Exception as exc:
                cleanup.append(f"QEMU ließ sich nicht beenden: {exc}")
        if log is not None:
            log.close()
        summary["serial_tail"] = _serial_tail(scratch / "serial.log")
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            logger.error("Boot-Drill-Arbeitsbereich %s blieb bestehen", scratch)
            cleanup.append(f"Arbeitsbereich {scratch} konnte nicht gelöscht werden")
        if cleanup:
            summary["cleanup_error"] = "; ".join(cleanup)


def run_boot_drill(
    *,
    trigger: str = "web",
    config_snapshot: Optional[Mapping[str, Any]] = None,
    run=subprocess.run,
    popen=subprocess.Popen,
    connect: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """Führt den Boot-Drill aus. Wirft nicht; Ergebnis im Job-Summary-Format."""
    cfg = (
        _SnapshotConfig(dict(config_snapshot))
        if config_snapshot is not None
        else get_config()
    )
    summary: dict[str, Any] = {
        "ok": False,
        "kind": JOB_KIND,
        "trigger": trigger,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "error": None,
        "bootable": False,
        "snapshot": None,
        "archive": None,
        "seconds_to_agent": None,
        "kvm": False,
        "serial_tail": "",
        "stale_cleaned": 0,
    }
    try:
        _drill(cfg, summary, run, popen, connect or _unix_connect)
    except RuntimeError as exc:
        summary["error"] = str(exc)
    except subprocess.TimeoutExpired as exc:
        summary["error"] = (
            f"Zeitüberschreitung bei proxmox-backup-client nach {int(exc.timeout)} s"
        )
    except Exception as exc:
        logger.exception("Boot-Drill fehlgeschlagen")
        summary["error"] = f"Boot-Drill fehlgeschlagen: {exc}"
    cleanup_error = summary.pop("cleanup_error", None)
    if cleanup_error:
        summary["error"] = "; ".join(filter(None, [summary["error"], cleanup_error]))
    summary["ok"] = bool(summary["bootable"]) and not summary["error"]
    summary["ended_at"] = datetime.now().isoformat(timespec="seconds")
    # Eine Zeile mit festem Schlüssel trägt die Historie für boot_drill_due.
    summary["pairs"] = [
        {
            "name": RUN_NAME,
            "ok": summary["ok"],
            "history_key": BOOT_DRILL_HISTORY_KEY,
            "trigger": trigger,
        }
    ]
    if summary["ok"]:
        notify(
            "boot_drill_ok",
            "Boot-Drill bestanden",
            f"{summary['snapshot']} bootet, Guest Agent nach "
            f"{summary['seconds_to_agent']} s",
        )
    else:
        notify("boot_drill_error", "Boot-Drill fehlgeschlagen", str(summary["error"]))
    return summary


def boot_drill_due(cfg, db, *, now: Optional[float] = None) -> dict[str, Any]:
    """Fälligkeit des Boot-Drills über einen globalen Historienschlüssel."""
    settings = boot_drill_settings(cfg)
    if not settings["enabled"] or not pbs_settings(cfg).get("enabled"):
        return {"due": False, "reason": "disabled"}
    schedule = settings["schedule"]
    if _is_disabled(schedule):
        return {"due": False, "reason": f"schedule={schedule}"}
    if not _is_valid_schedule(schedule):
        return {"due": False, "reason": "invalid_schedule", "error": schedule}
    backup = (
        cfg.get("backup") if isinstance(cfg, Mapping) else cfg.get("backup", default={})
    ) or {}
    timezone_name = str(backup.get("timezone") or DEFAULT_TIMEZONE)
    retry_sec = 60 * bounded_int(
        backup.get("scheduler_retry_minutes", 60), default=60, minimum=1, maximum=10080
    )
    grace_minutes = bounded_int(
        backup.get("scheduler_grace_minutes", 15), default=15, minimum=1, maximum=1440
    )
    now_value = float(time.time() if now is None else now)
    try:
        history = _load_history(db, {BOOT_DRILL_HISTORY_KEY: BOOT_DRILL_HISTORY_KEY})
        evaluation = _evaluate_due(
            schedule,
            history.get(BOOT_DRILL_HISTORY_KEY) or {},
            history_key=BOOT_DRILL_HISTORY_KEY,
            now=now_value,
            timezone_name=timezone_name,
            retry_sec=retry_sec,
            grace_minutes=grace_minutes,
            run_on_first_tick=False,
        )
    except Exception as exc:
        return {"due": False, "error": str(exc)}
    return {
        "history_key": BOOT_DRILL_HISTORY_KEY,
        "schedule": schedule,
        "timezone": timezone_name,
        "next_run": next_run_after(
            schedule, after=now_value, timezone_name=timezone_name
        ),
        **evaluation,
    }


__all__ = [
    "BOOT_DRILL_HISTORY_KEY",
    "JOB_KIND",
    "RUN_NAME",
    "boot_drill_due",
    "boot_drill_settings",
    "run_boot_drill",
]
