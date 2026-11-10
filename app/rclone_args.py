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
from pathlib import Path
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
    rf"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\]\)}}\"']+)",
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


def normalize_filter_aliases(args: list[str]) -> list[str]:
    """Make the short filter flag pass through the same mandatory rule compiler."""
    normalized: list[str] = []
    value_flags = {
        "--include",
        "--include-from",
        "--exclude",
        "--exclude-from",
        "--filter",
        "--filter-from",
        "--filters-file",
        "--files-from",
        "--files-from-raw",
        "--files-from0",
    }
    index = 0
    while index < len(args):
        token = args[index]
        if token in value_flags or token == "-f":
            normalized.append("--filter" if token == "-f" else token)
            if index + 1 < len(args):
                normalized.append(args[index + 1])
                index += 1
        elif token.startswith("-f="):
            normalized.append("--filter=" + token[3:])
        elif token.startswith("-f") and not token.startswith("--"):
            normalized.append("--filter=" + token[2:])
        elif re.match(r"^-[A-Za-z]*f", token) and not token.startswith("--"):
            raise UnsafeRcloneArgument(
                "Kombinierte kurze Filter-Flags sind nicht erlaubt; --filter verwenden"
            )
        else:
            normalized.append(token)
        index += 1
    return normalized


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
    normalized = normalize_filter_aliases([str(token) for token in args])
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


DANGEROUS_RCLONE_CONFIG_KEYS = frozenset(
    {
        "password_command",
        "token_command",
        "metadata_mapper",
        "sftp_ssh",
        "ssh",
        "key_pem_command",
        "client_cert_command",
    }
)


class UnsafeRcloneConfigError(ValueError):
    """Wird geworfen, wenn rclone.conf ausführbare Direktiven enthält."""


def resolve_rclone_config_path(explicit: Path | str | None = None) -> Path | None:
    """Ermittelt die aktive rclone.conf ohne Aufruf externer Prozesse."""
    from pathlib import Path as _Path

    candidates: list[_Path] = []
    if explicit:
        candidates.append(_Path(explicit).expanduser())
    env_cfg = os.environ.get("RCLONE_CONFIG", "").strip()
    if env_cfg:
        candidates.append(_Path(env_cfg).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        candidates.append(_Path(xdg).expanduser() / "rclone" / "rclone.conf")
    try:
        candidates.append(_Path.home() / ".config" / "rclone" / "rclone.conf")
    except (RuntimeError, OSError):
        pass
    candidates.extend(
        [
            _Path("/root/.config/rclone/rclone.conf"),
            _Path("/etc/rclone/rclone.conf"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _parse_oauth_expiry(raw_token: str, *, now: float) -> dict[str, Any] | None:
    """Analysiert das expiry-Feld eines rclone-OAuth-Tokens ohne Secret-Leak."""
    from datetime import datetime, timezone
    import json

    text = str(raw_token or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    expiry_raw = str(payload.get("expiry") or "").strip()
    if not expiry_raw or expiry_raw.startswith("0001-01-01"):
        return None
    normalized = expiry_raw[:-1] + "+00:00" if expiry_raw.endswith("Z") else expiry_raw
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized)
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    expiry_ts = dt.timestamp()
    hours_remaining = (expiry_ts - now) / 3600.0
    has_refresh = bool(str(payload.get("refresh_token") or "").strip())
    return {
        "expiry": dt.astimezone(timezone.utc).isoformat(),
        "expiry_ts": expiry_ts,
        "hours_remaining": round(hours_remaining, 1),
        "expired": hours_remaining <= 0,
        "has_refresh_token": has_refresh,
        "needs_attention": (
            (not has_refresh and hours_remaining <= 72.0) or hours_remaining <= -24.0
        ),
    }


def inspect_rclone_config(
    config_path: Any = None, *, now: float | None = None
) -> dict[str, Any]:
    """Prüft rclone.conf auf gefährliche Ausführungsdirektiven, Dateirechte und OAuth-Ablauf."""
    import stat
    import time

    current_now = float(time.time() if now is None else now)
    path = resolve_rclone_config_path(config_path)
    if path is None:
        return {
            "found": False,
            "path": None,
            "ok": True,
            "permissions_ok": True,
            "mode_octal": None,
            "dangerous_directives": [],
            "expiring_tokens": [],
            "remotes": [],
        }

    permissions_ok = True
    mode_octal: str | None = None
    try:
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        mode_octal = f"0{mode:03o}"
        if os.name != "nt" and (mode & 0o077) != 0:
            permissions_ok = False
    except OSError:
        pass

    dangerous_directives: list[dict[str, str]] = []
    expiring_tokens: list[dict[str, Any]] = []
    remotes: list[str] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "found": True,
            "path": str(path),
            "ok": False,
            "error": str(exc),
            "permissions_ok": permissions_ok,
            "mode_octal": mode_octal,
            "dangerous_directives": [],
            "expiring_tokens": [],
            "remotes": [],
        }

    current_section = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            if current_section and current_section not in remotes:
                remotes.append(current_section)
            continue
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key_clean = key.strip().lower()
        val_clean = raw_val.strip()
        if not val_clean:
            continue
        if key_clean in DANGEROUS_RCLONE_CONFIG_KEYS or key_clean.endswith("_command"):
            dangerous_directives.append(
                {
                    "remote": current_section or "global",
                    "key": key_clean,
                }
            )
        elif key_clean == "token" and current_section:
            token_info = _parse_oauth_expiry(val_clean, now=current_now)
            if token_info and token_info.get("needs_attention"):
                expiring_tokens.append(
                    {
                        "remote": current_section,
                        **token_info,
                    }
                )

    return {
        "found": True,
        "path": str(path),
        "ok": len(dangerous_directives) == 0,
        "permissions_ok": permissions_ok,
        "mode_octal": mode_octal,
        "dangerous_directives": dangerous_directives,
        "expiring_tokens": expiring_tokens,
        "remotes": remotes,
    }


def assert_safe_rclone_config(
    config_path: Any = None,
) -> dict[str, Any]:
    """Bricht fail-closed ab, wenn rclone.conf ausführbare Direktiven enthält."""
    report = inspect_rclone_config(config_path)
    dangerous = report.get("dangerous_directives") or []
    if dangerous:
        items = ", ".join(f"{item['remote']}.{item['key']}" for item in dangerous[:5])
        raise UnsafeRcloneConfigError(
            f"rclone.conf enthält gesperrte Ausführungs-Direktiven ({items})"
        )
    return report
