"""Sichere, allowlist-basierte Pre- und Post-Backup-Hooks ohne Shell-Ausführung.

Hooks dürfen niemals als freie Shell-Befehle in der YAML-Konfiguration stehen.
Erlaubt sind ausschließlich Dateinamen aus einem geschützten Hook-Verzeichnis
(Standard: ``/etc/rclone-sync/hooks.d``), die keine Symlinks sind, auf POSIX
weder gruppen- noch weltbeschreibbar sind und dem Dienstnutzer oder ``root``
gehören.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Mapping

from .rclone_args import redact_command_text

logger = logging.getLogger(__name__)

DEFAULT_HOOKS_DIR = "/etc/rclone-sync/hooks.d"
HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_HOOK_TIMEOUT_SECONDS = 60


class HookSecurityError(ValueError):
    """Wird ausgelöst, wenn ein Hook-Skript Sicherheitsanforderungen verletzt."""


def validate_hook_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text or "\\" in text or ".." in text or not HOOK_NAME_RE.fullmatch(text):
        raise ValueError(
            "Hook-Name darf nur ein einfacher Dateiname (A-Z, a-z, 0-9, ., _, -) sein"
        )
    return text


def hooks_dir_from_config(config_snapshot: Mapping[str, Any] | None = None) -> Path:
    paths = (
        config_snapshot.get("paths") if isinstance(config_snapshot, Mapping) else None
    )
    raw = (
        str(paths.get("hooks_dir") or "").strip() if isinstance(paths, Mapping) else ""
    )
    return Path(raw or DEFAULT_HOOKS_DIR)


def inspect_hook_file(script_path: Path) -> dict[str, Any]:
    """Prüft ein einzelnes Hook-Skript auf Symlink-Freiheit, Eigentümer und Rechte."""
    name = script_path.name
    if not HOOK_NAME_RE.fullmatch(name):
        return {
            "name": name,
            "ok": False,
            "reason": "Ungültiger Dateiname",
        }
    try:
        if script_path.is_symlink():
            return {
                "name": name,
                "ok": False,
                "reason": "Symbolische Links sind für Hooks nicht erlaubt",
            }
        st = script_path.lstat()
    except OSError as exc:
        return {
            "name": name,
            "ok": False,
            "reason": f"Datei nicht lesbar: {exc}",
        }
    if not stat.S_ISREG(st.st_mode):
        return {
            "name": name,
            "ok": False,
            "reason": "Kein reguläres Skript",
        }
    mode = stat.S_IMODE(st.st_mode)
    if os.name != "nt":
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            return {
                "name": name,
                "ok": False,
                "mode": f"0{mode:03o}",
                "reason": "Skript darf nicht gruppen- oder weltbeschreibbar sein",
            }
        allowed_uids = {0}
        getuid = getattr(os, "getuid", None)
        if callable(getuid):
            allowed_uids.add(int(getuid()))
        if int(st.st_uid) not in allowed_uids:
            return {
                "name": name,
                "ok": False,
                "mode": f"0{mode:03o}",
                "reason": "Skript muss root oder dem Dienstnutzer gehören",
            }
        if not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
            return {
                "name": name,
                "ok": False,
                "mode": f"0{mode:03o}",
                "reason": "Skript ist nicht als ausführbar markiert (chmod +x)",
            }
    return {
        "name": name,
        "ok": True,
        "mode": f"0{mode:03o}",
        "size": int(st.st_size),
        "reason": "ok",
    }


def list_available_hooks(
    config_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Listet alle im Hook-Verzeichnis gefundenen Skripte samt Sicherheitsstatus."""
    directory = hooks_dir_from_config(config_snapshot)
    try:
        if directory.is_symlink() or not directory.is_dir():
            return {
                "directory": str(directory),
                "exists": False,
                "hooks": [],
            }
        entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return {
            "directory": str(directory),
            "exists": False,
            "hooks": [],
        }
    hooks: list[dict[str, Any]] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        hooks.append(inspect_hook_file(entry))
    return {
        "directory": str(directory),
        "exists": True,
        "hooks": hooks,
    }


def run_hook(
    hook_name: str,
    *,
    phase: str,
    pair_name: str = "",
    direction: str = "",
    mode: str = "",
    dry_run: bool = False,
    status: str = "",
    config_snapshot: Mapping[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_HOOK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Führt einen allowlist-geprüften Hook ohne Shell mit bereinigter Umgebung aus."""
    clean_name = validate_hook_name(hook_name)
    if not clean_name:
        return {"ok": True, "skipped": True, "hook": ""}
    directory = hooks_dir_from_config(config_snapshot)
    if directory.is_symlink() or not directory.is_dir():
        raise HookSecurityError(
            f"Hook-Verzeichnis fehlt oder ist unsicher: {directory}"
        )
    if os.name != "nt":
        try:
            dir_st = directory.lstat()
            if stat.S_IMODE(dir_st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
                raise HookSecurityError(
                    f"Hook-Verzeichnis darf nicht gruppen-/weltbeschreibbar sein: {directory}"
                )
        except OSError as exc:
            raise HookSecurityError(f"Hook-Verzeichnis nicht prüfbar: {exc}") from exc

    script_path = directory / clean_name
    inspection = inspect_hook_file(script_path)
    if not inspection.get("ok"):
        raise HookSecurityError(
            f"Hook {clean_name!r} abgelehnt: {inspection.get('reason')}"
        )

    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "SICHERPFAD_HOOK_PHASE": str(phase or "pre"),
        "SICHERPFAD_PAIR_NAME": str(pair_name or ""),
        "SICHERPFAD_DIRECTION": str(direction or ""),
        "SICHERPFAD_MODE": str(mode or ""),
        "SICHERPFAD_DRY_RUN": "1" if dry_run else "0",
        "SICHERPFAD_STATUS": str(status or ""),
    }
    started = time.time()
    try:
        completed = subprocess.run(
            [str(script_path)],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout_seconds), 600)),
            env=env,
            cwd=str(directory),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "hook": clean_name,
            "phase": phase,
            "returncode": -1,
            "duration_sec": round(time.time() - started, 3),
            "error": f"Hook {clean_name!r} hat das Zeitlimit ({timeout_seconds}s) überschritten",
            "stdout": redact_command_text(str(exc.stdout or ""))[:1000],
            "stderr": redact_command_text(str(exc.stderr or ""))[:1000],
        }
    except OSError as exc:
        return {
            "ok": False,
            "hook": clean_name,
            "phase": phase,
            "returncode": -1,
            "duration_sec": round(time.time() - started, 3),
            "error": f"Hook {clean_name!r} konnte nicht gestartet werden: {exc}",
            "stdout": "",
            "stderr": "",
        }

    stdout_clean = redact_command_text(completed.stdout or "").strip()[:1000]
    stderr_clean = redact_command_text(completed.stderr or "").strip()[:1000]
    ok = completed.returncode == 0
    return {
        "ok": ok,
        "hook": clean_name,
        "phase": phase,
        "returncode": int(completed.returncode),
        "duration_sec": round(time.time() - started, 3),
        "stdout": stdout_clean,
        "stderr": stderr_clean,
        "error": (
            None
            if ok
            else f"Hook {clean_name!r} beendet mit Exit-Code {completed.returncode}"
            + (f": {stderr_clean}" if stderr_clean else "")
        ),
    }
