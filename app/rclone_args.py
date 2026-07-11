"""Validierung frei konfigurierbarer rclone-Argumente.

Die Anwendung startet rclone ohne Shell; Command-Injection ist damit ausgeschlossen.
Einige globale rclone-Flags könnten aber Schutzpfade, Konfiguration, Logging oder RC-
Server überschreiben. Diese Flags bleiben standardmäßig der Anwendung vorbehalten.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

_MAX_ARGS = 256
_MAX_ARG_LENGTH = 4096

_EXACT_BLOCKED = {
    "--",
    "--config",
    "--cache-dir",
    "--workdir",
    "--temp-dir",
    "--log-file",
    "--syslog",
    "--log-systemd",
    "--password-command",
    "--ask-password",
    "--dry-run",
    "--resync",
    "--resync-mode",
    "--backup-dir",
    "--backup-dir1",
    "--backup-dir2",
    "--filter-from",
    "--filters-file",
    "--include-from",
    "--exclude-from",
    "--files-from",
    "--files-from-raw",
    "--max-delete",
    "--max-delete-size",
    "--delete-excluded",
    "--ignore-errors",
    "--copy-dest",
    "--compare-dest",
    "--sftp-ssh",
    "--sftp-key-file",
    "--sftp-known-hosts-file",
}
_BLOCKED_PREFIXES = (
    "--rc",
    "--dump",
    "--config=",
    "--cache-dir=",
    "--workdir=",
    "--temp-dir=",
    "--log-file=",
    "--syslog=",
    "--password-command=",
    "--backup-dir=",
    "--backup-dir1=",
    "--backup-dir2=",
    "--filter-from=",
    "--filters-file=",
    "--include-from=",
    "--exclude-from=",
    "--files-from=",
    "--files-from-raw=",
    "--max-delete=",
    "--max-delete-size=",
    "--copy-dest=",
    "--compare-dest=",
    "--sftp-ssh=",
    "--sftp-key-file=",
    "--sftp-known-hosts-file=",
)


def rclone_subprocess_env() -> dict[str, str]:
    """Verhindert interaktive Passwortprompts in Web-, Timer- und CLI-Prozessen."""
    return {**os.environ, "RCLONE_ASK_PASSWORD": "false"}


class UnsafeRcloneArgument(ValueError):
    pass


def parse_rclone_args(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("rclone_args müssen Text oder eine Textliste sein")
        try:
            out.extend(shlex.split(item))
        except ValueError as exc:
            raise ValueError(f"Ungültige rclone_args: {item!r}: {exc}") from exc
    if len(out) > _MAX_ARGS:
        raise ValueError(f"Zu viele rclone-Argumente (maximal {_MAX_ARGS})")
    for token in out:
        if not token or len(token) > _MAX_ARG_LENGTH:
            raise ValueError("Leeres oder zu langes rclone-Argument")
        if any(char in token for char in ("\x00", "\r", "\n")):
            raise ValueError("rclone-Argument enthält Steuerzeichen")
    return out


def blocked_arguments(args: list[str]) -> list[str]:
    blocked: list[str] = []
    for token in args:
        flag = token.split("=", 1)[0].lower()
        lowered = token.lower()
        if flag in _EXACT_BLOCKED or any(
            lowered.startswith(prefix) for prefix in _BLOCKED_PREFIXES
        ):
            blocked.append(token)
    return list(dict.fromkeys(blocked))


def validate_parsed_rclone_args(
    args: list[str], *, allow_unsafe: bool = False
) -> list[str]:
    """Validiert bereits tokenisierte Argumente, ohne Werte mit Leerzeichen neu zu splitten."""
    if len(args) > _MAX_ARGS:
        raise ValueError(f"Zu viele rclone-Argumente (maximal {_MAX_ARGS})")
    normalized = [str(token) for token in args]
    for token in normalized:
        if not token or len(token) > _MAX_ARG_LENGTH:
            raise ValueError("Leeres oder zu langes rclone-Argument")
        if any(char in token for char in ("\x00", "\r", "\n")):
            raise ValueError("rclone-Argument enthält Steuerzeichen")
    blocked = blocked_arguments(normalized)
    if blocked and not allow_unsafe:
        raise UnsafeRcloneArgument(
            "Geschützte rclone-Flags sind nicht erlaubt: " + ", ".join(blocked)
        )
    return normalized


def validate_rclone_args(value: Any, *, allow_unsafe: bool = False) -> list[str]:
    return validate_parsed_rclone_args(
        parse_rclone_args(value), allow_unsafe=allow_unsafe
    )
