"""Validierung frei konfigurierbarer rclone-Argumente.

Die Anwendung startet rclone ohne Shell. Einige rclone-Flags können trotzdem externe
Programme ausführen oder Schutzpfade, Konfiguration, Logging beziehungsweise RC-Server
überschreiben. Ausführungs- und Zugangsdaten-Flags bleiben immer gesperrt; weitere
geschützte Flags können ausschließlich im expliziten Expertenmodus verwendet werden.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

_MAX_ARGS = 256
_MAX_ARG_LENGTH = 4096
_BLOCKED_SHORT_OPTIONS = {"n", "i"}

_EXECUTION_FLAGS = {"--metadata-mapper"}

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
    "-n",
    "--interactive",
    "-i",
    "--filter",
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
_REDACTED = "***REDACTED***"
_SENSITIVE_FLAG = (
    r"-{1,2}(?=[A-Za-z0-9-]*(?:password|passwd|pass|secret|token|credential|"
    r"access-key|private-key|customer-key|encryption-key|sas-url|header|key))"
    r"[A-Za-z0-9][A-Za-z0-9-]*"
)
_SENSITIVE_FLAG_VALUE_RE = re.compile(
    rf"(?P<flag>{_SENSITIVE_FLAG})"
    rf"(?P<separator>\s*=\s*|\s+)"
    rf"(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;\]\)}}\"']+)",
    re.IGNORECASE,
)
_SENSITIVE_FLAG_NAME_RE = re.compile(rf"^{_SENSITIVE_FLAG}$", re.IGNORECASE)
_URL_PASSWORD_RE = re.compile(
    r"(?P<prefix>://[^/\s:@]+:)(?P<password>[^@\s/]+)(?=@)",
    re.IGNORECASE,
)


def redact_command_text(text: str) -> str:
    """Maskiert Zugangsdaten in bereits formatierten rclone-Kommandos.

    Die Funktion arbeitet auch auf eingebetteten Command-Strings in JSON, CSV
    oder Benachrichtigungs-Payloads und deckt sowohl ``--flag=value`` als auch
    ``--flag value`` ab. Der Flag-Name bleibt für die Diagnose sichtbar.
    """

    value = str(text or "")

    def replace_flag(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        if raw_value.startswith('"') and raw_value.endswith('"'):
            replacement = f'"{_REDACTED}"'
        elif raw_value.startswith("'") and raw_value.endswith("'"):
            replacement = f"'{_REDACTED}'"
        else:
            replacement = _REDACTED
        return f"{match.group('flag')}{match.group('separator')}{replacement}"

    value = _SENSITIVE_FLAG_VALUE_RE.sub(replace_flag, value)
    return _URL_PASSWORD_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", value
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
        short_cluster = (
            flag.startswith("-")
            and not flag.startswith("--")
            and 2 <= len(flag[1:]) <= 4
            and flag[1:].isalpha()
            and any(option in flag[1:] for option in _BLOCKED_SHORT_OPTIONS)
        )
        if (
            flag in _EXACT_BLOCKED
            or short_cluster
            or any(lowered.startswith(prefix) for prefix in _BLOCKED_PREFIXES)
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
    execution_flags = [
        token.split("=", 1)[0]
        for token in normalized
        if token.split("=", 1)[0].lower() in _EXECUTION_FLAGS
    ]
    if execution_flags:
        raise UnsafeRcloneArgument(
            "rclone-Flags zur Ausführung externer Programme sind auch im "
            "Expertenmodus nicht erlaubt: " + ", ".join(dict.fromkeys(execution_flags))
        )
    credential_flags = [
        token.split("=", 1)[0]
        for token in normalized
        if _SENSITIVE_FLAG_NAME_RE.fullmatch(token.split("=", 1)[0])
    ]
    if credential_flags:
        raise UnsafeRcloneArgument(
            "Zugangsdaten-Flags sind auch im Expertenmodus nicht erlaubt; "
            "Secrets müssen über rclone.conf oder geschützte Umgebungsvariablen "
            "bereitgestellt werden: " + ", ".join(dict.fromkeys(credential_flags))
        )
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
