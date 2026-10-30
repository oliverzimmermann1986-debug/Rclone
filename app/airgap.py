"""Airgap-USB: Backup-Disk nur für die Dauer eines Jobs einbinden, danach auswerfen.

Mount, Unmount und Power-off laufen über udisksctl (D-Bus, kein sudo).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
from typing import Any, Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

_PACKAGES = {"udisksctl": "udisks2", "findmnt": "util-linux"}
_MOUNTED_AT = re.compile(r"^Mounted \S+ at (.+?)\.?$")


def validate_device(device: Any) -> str:
    """Prüft den Gerätepfad (Blockgerät unter /dev/); sonst ValueError."""
    if (
        not isinstance(device, str)
        or not device.startswith("/dev/")
        or len(device) > 256
        or any(c.isspace() or not c.isprintable() for c in device)
        or ".." in device.split("/")
    ):
        raise ValueError(
            "Ungültiges Airgap-Gerät: erwartet ein Blockgerät unter /dev/ "
            "(z. B. /dev/disk/by-uuid/XXXX), höchstens 256 Zeichen, ohne "
            "Leerzeichen, Steuerzeichen oder „..“."
        )
    return device


def _exec(cmd: list[str], timeout: int, run: Callable[..., Any]) -> Any:
    """Führt ein Kommando aus; Systemfehler werden zu RuntimeError mit deutscher Meldung."""
    try:
        return run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"{cmd[0]} nicht installiert (Paket {_PACKAGES.get(cmd[0], cmd[0])})"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{cmd[0]} {cmd[1]}: Zeitüberschreitung nach {timeout} s"
        ) from None
    except OSError as exc:
        raise RuntimeError(f"{cmd[0]} nicht ausführbar: {exc}") from None


def _short(proc: Any) -> str:
    text = proc.stderr or proc.stdout or f"Exit-Code {proc.returncode}"
    return " ".join(str(text).split())[:300]


def device_mountpoint(
    device: str, *, run: Callable[..., Any] = subprocess.run
) -> str | None:
    """Aktueller Mountpoint des Geräts oder None, wenn nicht eingebunden."""
    validate_device(device)
    proc = _exec(["findmnt", "-n", "-o", "TARGET", "--source", device], 30, run)
    first_line = (proc.stdout or "").strip().partition("\n")[0].strip()
    return first_line or None


def mount_device(device: str, *, run: Callable[..., Any] = subprocess.run) -> str:
    """Bindet das Gerät per udisksctl ein und liefert den Mountpoint."""
    validate_device(device)
    proc = _exec(["udisksctl", "mount", "-b", device, "--no-user-interaction"], 60, run)
    if proc.returncode == 0:
        found = _MOUNTED_AT.match((proc.stdout or "").strip())
        mountpoint = found.group(1) if found else device_mountpoint(device, run=run)
    elif "AlreadyMounted" in (proc.stderr or ""):
        mountpoint = device_mountpoint(device, run=run)
    else:
        raise RuntimeError(
            f"Airgap-Gerät {device} konnte nicht eingebunden werden: {_short(proc)}"
        )
    if not mountpoint:
        raise RuntimeError(f"Mountpoint von {device} nicht ermittelbar")
    return mountpoint


def eject_device(
    device: str, *, run: Callable[..., Any] = subprocess.run
) -> dict[str, Any]:
    """sync, unmount, power-off. Wirft nie; Teilfehler stehen in `error`."""
    out: dict[str, Any] = {"unmounted": False, "powered_off": False, "error": None}
    errors: list[str] = []
    with contextlib.suppress(AttributeError, OSError):  # os.sync fehlt unter Windows
        os.sync()  # best effort, unmount schreibt ohnehin zurück
    try:
        validate_device(device)
        proc = _exec(
            ["udisksctl", "unmount", "-b", device, "--no-user-interaction"], 120, run
        )
        # NotMounted: schon ausgehängt, Ziel erreicht
        out["unmounted"] = proc.returncode == 0 or "NotMounted" in (proc.stderr or "")
        if not out["unmounted"]:
            errors.append(
                f"Unmount fehlgeschlagen: {_short(proc)}; Power-off übersprungen"
            )
        else:
            proc = _exec(
                ["udisksctl", "power-off", "-b", device, "--no-user-interaction"],
                120,
                run,
            )
            out["powered_off"] = proc.returncode == 0
            if not out["powered_off"]:
                errors.append(f"Power-off fehlgeschlagen: {_short(proc)}")
    except (RuntimeError, ValueError) as exc:
        errors.append(str(exc))
    out["error"] = "; ".join(errors) or None
    return out


@contextlib.contextmanager
def airgap_session(
    pair: Mapping[str, Any], *, run: Callable[..., Any] = subprocess.run
) -> Iterator[dict[str, Any]]:
    """Gerät des Pairs für den Job einbinden und danach immer auswerfen."""
    device = pair.get("airgap_device") or ""
    if not device:
        yield {"active": False}
        return
    validate_device(device)
    mountpoint = mount_device(device, run=run)
    result: dict[str, Any] = {
        "active": True,
        "device": device,
        "mountpoint": mountpoint,
    }
    try:
        if not _is_under(pair.get("local"), mountpoint):
            raise RuntimeError("Lokaler Pfad liegt nicht auf dem Airgap-Gerät")
        yield result
    finally:
        result["eject"] = eject_device(device, run=run)
        if result["eject"]["error"]:
            logger.warning("Airgap-Auswurf unvollständig: %s", result["eject"]["error"])


def _is_under(path: Any, base: str) -> bool:
    if not path:  # realpath("") wäre das Arbeitsverzeichnis
        return False
    path, base = os.path.realpath(str(path)), os.path.realpath(base)
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:  # z. B. verschiedene Laufwerke
        return False
