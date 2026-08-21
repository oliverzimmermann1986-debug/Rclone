"""Validierung und Normalisierung der editierbaren YAML-Konfiguration."""

from __future__ import annotations

import copy
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from croniter import croniter

from .job_definitions import legacy_job_definitions, stable_job_id
from .rclone_args import UnsafeRcloneArgument, validate_rclone_args
from .security import (
    DEFAULT_HIDDEN_REMOTE_PATHS,
    is_hidden_remote_path,
    normalize_hidden_remote_paths,
    normalize_remote_path,
)

_KNOWN_SECTIONS = frozenset(
    {"web", "paths", "backup", "notifications", "pbs", "maintenance", "schema_version"}
)
_KNOWN_WEB_KEYS = frozenset(
    {
        "username",
        "password",
        "password_hash",
        "secret_key",
        "session_version",
        "session_max_age_seconds",
        "allowed_hosts",
        "local_browse_roots",
        "hidden_remote_paths",
        "secure_cookie",
        "hsts_seconds",
        "login_window_seconds",
        "login_max_failures",
        "login_lock_seconds",
    }
)

_DISABLED_SCHEDULES = {"", "off", "manual", "disabled", "none"}
_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,80}$")
_STABLE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PBS_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(?:ms|s|m|h|d|w)?$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}:.*$")
_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAX_PAIRS = 256
_MAX_JOBS = 256
_MAX_PBS_TARGETS = 128
_MAX_WEBHOOKS = 64
_MAX_PBS_PATHS = 64
CONFIG_SCHEMA_VERSION = 3
_NOTIFICATION_EVENTS = {
    "sync_started",
    "sync_ok",
    "sync_error",
    "conflict",
    "mount_check_failed",
    "cancelled",
    "pair_overdue",
    "restore_test_ok",
    "restore_test_error",
}


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _number(
    value: Any, *, default: float, minimum: float, maximum: float, integer: bool = False
):
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        parsed = int(default) if integer else float(default)
    return max(minimum, min(maximum, parsed))


def _boolean(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein", ""}:
        return False
    return default


def _clean_path(value: Any) -> str:
    text = str(value or "").strip()
    if any(ch in text for ch in ("\x00", "\n", "\r")):
        raise ValueError("enthält Steuerzeichen")
    return text


def _is_remote(path: str) -> bool:
    return bool(
        path
        and not Path(path).is_absolute()
        and _REMOTE_RE.match(path)
        and not path.startswith("-")
    )


def _is_absolute_local(path: str) -> bool:
    # Accept deployment-style POSIX paths even when validating a config from a
    # Windows development host; also accept native absolute paths for tests.
    return path.startswith("/") or (os.name == "nt" and Path(path).is_absolute())


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("muss Text oder Liste sein")


def _paths_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if _is_remote(a) != _is_remote(b):
        return False
    if _is_remote(a):
        aa = a.rstrip("/")
        bb = b.rstrip("/")
        return aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/")
    try:
        pa = Path(a).expanduser().resolve()
        pb = Path(b).expanduser().resolve()
        return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)
    except (OSError, RuntimeError, ValueError):
        return False


def _valid_cron(expression: str) -> bool:
    """Only accept the five-field minute-based format used by the systemd timer."""
    return len(expression.split()) == 5 and croniter.is_valid(expression)


def validate_config(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(data, dict):
        raise ConfigValidationError(["Config-Root muss ein Mapping sein"])

    cfg = copy.deepcopy(data)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        schema_version = int(cfg.get("schema_version", 1) or 1)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version < 1 or schema_version > CONFIG_SCHEMA_VERSION:
        errors.append(
            f"schema_version {schema_version} wird nicht unterstützt "
            f"(erwartet 1 bis {CONFIG_SCHEMA_VERSION})"
        )
    cfg["schema_version"] = CONFIG_SCHEMA_VERSION

    web = cfg.setdefault("web", {})
    if not isinstance(web, dict):
        errors.append("web muss ein Mapping sein")
        web = cfg["web"] = {}
    username = str(web.get("username") or "admin").strip()
    if not username or len(username) > 128 or any(c in username for c in "\r\n\x00"):
        errors.append("web.username ist ungültig")
    web["username"] = username or "admin"
    secure_cookie = web.get("secure_cookie", False)
    if isinstance(secure_cookie, str) and secure_cookie.strip().lower() == "auto":
        web["secure_cookie"] = "auto"
    else:
        web["secure_cookie"] = _boolean(secure_cookie, default=False)
    web["session_version"] = int(
        _number(
            web.get("session_version", 1),
            default=1,
            minimum=1,
            maximum=2_147_483_647,
            integer=True,
        )
    )
    web["session_max_age_seconds"] = int(
        _number(
            web.get("session_max_age_seconds", 604800),
            default=604800,
            minimum=300,
            maximum=2_592_000,
            integer=True,
        )
    )
    web["login_window_seconds"] = int(
        _number(
            web.get("login_window_seconds", 300),
            default=300,
            minimum=60,
            maximum=86_400,
            integer=True,
        )
    )
    web["login_max_failures"] = int(
        _number(
            web.get("login_max_failures", 10),
            default=10,
            minimum=3,
            maximum=100,
            integer=True,
        )
    )
    web["login_lock_seconds"] = int(
        _number(
            web.get("login_lock_seconds", 900),
            default=900,
            minimum=60,
            maximum=86_400,
            integer=True,
        )
    )
    web["hsts_seconds"] = int(
        _number(
            web.get("hsts_seconds", 0),
            default=0,
            minimum=0,
            maximum=63_072_000,
            integer=True,
        )
    )
    try:
        allowed_hosts = _normalize_string_list(web.get("allowed_hosts", ["*"]))
    except ValueError:
        errors.append("web.allowed_hosts muss Text oder Liste sein")
        allowed_hosts = ["*"]
    clean_hosts: list[str] = []
    for host in allowed_hosts:
        if len(host) > 255 or any(ch in host for ch in ("/", "\\", "\x00", "\r", "\n")):
            errors.append(f"Ungültiger Host in web.allowed_hosts: {host}")
        else:
            clean_hosts.append(host.casefold())
    web["allowed_hosts"] = list(dict.fromkeys(clean_hosts or ["*"]))
    roots = web.get(
        "local_browse_roots", ["/mnt", "/media", "/srv", "/opt/rclone-sync/data"]
    )
    try:
        roots = _normalize_string_list(roots)
    except ValueError:
        errors.append("web.local_browse_roots muss Text oder Liste sein")
        roots = []
    clean_roots: list[str] = []
    for root in roots:
        try:
            root = _clean_path(root)
            if not _is_absolute_local(root):
                errors.append(f"Browser-Root muss absolut sein: {root}")
            else:
                clean_roots.append(root.rstrip("/") or "/")
        except ValueError as exc:
            errors.append(f"Browser-Root {exc}")
    web["local_browse_roots"] = list(dict.fromkeys(clean_roots))

    hidden_raw = web.get("hidden_remote_paths", list(DEFAULT_HIDDEN_REMOTE_PATHS))
    try:
        hidden_list = _normalize_string_list(hidden_raw)
    except ValueError:
        errors.append("web.hidden_remote_paths muss Text oder Liste sein")
        hidden_list = list(DEFAULT_HIDDEN_REMOTE_PATHS)
    clean_hidden: list[str] = []
    for entry in hidden_list:
        if ":" not in entry:
            errors.append(
                f"web.hidden_remote_paths braucht 'remote:pfad', nicht {entry!r}"
            )
            continue
        clean_hidden.append(normalize_remote_path(entry))
    web["hidden_remote_paths"] = list(dict.fromkeys(clean_hidden))
    hidden_remote_paths = normalize_hidden_remote_paths(web["hidden_remote_paths"])

    paths = cfg.setdefault("paths", {})
    if not isinstance(paths, dict):
        errors.append("paths muss ein Mapping sein")
        paths = cfg["paths"] = {}
    for key, default in (
        ("data_dir", "/opt/rclone-sync/data"),
        ("logs_dir", "/opt/rclone-sync/logs"),
        ("temp_dir", "/opt/rclone-sync/temp"),
    ):
        try:
            value = _clean_path(paths.get(key, default)) or default
            if not _is_absolute_local(value):
                errors.append(f"paths.{key} muss absolut sein")
            paths[key] = value
        except ValueError as exc:
            errors.append(f"paths.{key} {exc}")

    backup = cfg.setdefault("backup", {})
    if not isinstance(backup, dict):
        errors.append("backup muss ein Mapping sein")
        backup = cfg["backup"] = {}
    backup["enabled"] = _boolean(backup.get("enabled", True), default=True)
    jobs_were_explicit = "jobs" in backup
    backup["max_parallel"] = int(
        _number(
            backup.get("max_parallel", 2),
            default=2,
            minimum=1,
            maximum=16,
            integer=True,
        )
    )
    backup["timeout_hours"] = float(
        _number(backup.get("timeout_hours", 4), default=4, minimum=0.1, maximum=168)
    )
    backup["collect_pre_post_stats"] = _boolean(
        backup.get("collect_pre_post_stats", False)
    )
    backup["auto_resync"] = _boolean(backup.get("auto_resync", False))
    backup["auto_resync_first_run"] = _boolean(
        backup.get("auto_resync_first_run", True), default=True
    )
    backup["recover"] = _boolean(backup.get("recover", True), default=True)
    backup["resilient"] = _boolean(backup.get("resilient", True), default=True)
    backup["allow_unsafe_rclone_args"] = _boolean(
        backup.get("allow_unsafe_rclone_args", False)
    )
    backup["allow_external_filter_files"] = _boolean(
        backup.get("allow_external_filter_files", False)
    )
    backup["require_delete_confirmation"] = _boolean(
        backup.get("require_delete_confirmation", True), default=True
    )
    backup["require_max_delete_for_sync"] = _boolean(
        backup.get("require_max_delete_for_sync", True), default=True
    )
    max_lock = str(backup.get("max_lock") or "2m").strip()
    if not _DURATION_RE.match(max_lock):
        errors.append("backup.max_lock ist ungültig")
    backup["max_lock"] = max_lock
    conflict = str(backup.get("conflict_resolve") or "auto").strip().lower()
    if conflict not in {
        "auto",
        "newer",
        "older",
        "larger",
        "smaller",
        "path1",
        "path2",
        "none",
    }:
        errors.append("backup.conflict_resolve ist ungültig")
        conflict = "auto"
    backup["conflict_resolve"] = conflict
    backup["run_on_first_tick"] = _boolean(backup.get("run_on_first_tick", False))
    backup["scheduler_retry_minutes"] = int(
        _number(
            backup.get("scheduler_retry_minutes", 60),
            default=60,
            minimum=1,
            maximum=10080,
            integer=True,
        )
    )
    backup["scheduler_grace_minutes"] = int(
        _number(
            backup.get("scheduler_grace_minutes", 15),
            default=15,
            minimum=1,
            maximum=1440,
            integer=True,
        )
    )
    overdue_alerts = backup.get("overdue_alerts")
    if not isinstance(overdue_alerts, dict):
        overdue_alerts = {}
    backup["overdue_alerts"] = {
        "enabled": _boolean(overdue_alerts.get("enabled", True)),
        "repeat_hours": int(
            _number(
                overdue_alerts.get("repeat_hours", 24),
                default=24,
                minimum=1,
                maximum=720,
                integer=True,
            )
        ),
    }
    restore_test = backup.get("restore_test")
    if not isinstance(restore_test, dict):
        restore_test = {}
    restore_schedule = str(restore_test.get("schedule") or "manual").strip()
    if (
        restore_schedule
        and restore_schedule.lower() not in _DISABLED_SCHEDULES
        and not _valid_cron(restore_schedule)
    ):
        errors.append(f"backup.restore_test.schedule ist ungültig: {restore_schedule}")
    backup["restore_test"] = {
        "enabled": _boolean(restore_test.get("enabled", False)),
        "schedule": restore_schedule or "manual",
        "sample_files": int(
            _number(
                restore_test.get("sample_files", 20),
                default=20,
                minimum=1,
                maximum=500,
                integer=True,
            )
        ),
        "max_total_mb": int(
            _number(
                restore_test.get("max_total_mb", 256),
                default=256,
                minimum=1,
                maximum=51_200,
                integer=True,
            )
        ),
        "max_scan_files": int(
            _number(
                restore_test.get("max_scan_files", 20_000),
                default=20_000,
                minimum=100,
                maximum=1_000_000,
                integer=True,
            )
        ),
    }
    timezone = str(backup.get("timezone") or "Europe/Berlin").strip()
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:
        errors.append(f"backup.timezone ist ungültig: {timezone}")
    backup["timezone"] = timezone

    default_schedule = str(backup.get("default_schedule") or "manual").strip()
    if default_schedule.lower() not in _DISABLED_SCHEDULES and not _valid_cron(
        default_schedule
    ):
        errors.append(f"backup.default_schedule ist ungültig: {default_schedule}")
    backup["default_schedule"] = default_schedule

    tuning = backup.setdefault("tuning", {})
    if not isinstance(tuning, dict):
        errors.append("backup.tuning muss ein Mapping sein")
        tuning = backup["tuning"] = {}
    stats_interval = str(tuning.get("stats_interval") or "10s").strip()
    if not _DURATION_RE.match(stats_interval):
        errors.append("backup.tuning.stats_interval ist ungültig")
    tuning["stats_interval"] = stats_interval
    for key, low, high in (
        ("transfers", 1, 128),
        ("checkers", 1, 256),
        ("retries", 0, 100),
        ("low_level_retries", 0, 1000),
        ("max_delete", -1, 10_000_000),
    ):
        if tuning.get(key) not in (None, ""):
            try:
                tuning[key] = int(
                    _number(
                        tuning[key],
                        default=low,
                        minimum=low,
                        maximum=high,
                        integer=True,
                    )
                )
            except Exception:
                errors.append(f"backup.tuning.{key} ist ungültig")

    try:
        backup["rclone_args"] = _normalize_string_list(backup.get("rclone_args"))
        validate_rclone_args(
            backup["rclone_args"], allow_unsafe=backup["allow_unsafe_rclone_args"]
        )
    except (ValueError, UnsafeRcloneArgument) as exc:
        errors.append(f"backup.rclone_args {exc}")

    filter_file = str(backup.get("filter_file") or "").strip()
    if filter_file:
        try:
            clean_filter_file = _clean_path(filter_file)
            filter_path = Path(clean_filter_file).expanduser()
            if not _is_absolute_local(clean_filter_file):
                errors.append("backup.filter_file muss absolut sein")
            elif not backup["allow_external_filter_files"]:
                data_root = (
                    Path(str(paths.get("data_dir") or "/opt/rclone-sync/data"))
                    .expanduser()
                    .resolve()
                )
                resolved = filter_path.resolve()
                if resolved != data_root and not resolved.is_relative_to(data_root):
                    errors.append(
                        "backup.filter_file muss innerhalb paths.data_dir liegen"
                    )
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append(f"backup.filter_file ist ungültig: {exc}")
    backup["filter_file"] = filter_file

    pairs = backup.get("pairs") or []
    if not isinstance(pairs, list):
        errors.append("backup.pairs muss eine Liste sein")
        pairs = []
    elif len(pairs) > _MAX_PAIRS:
        errors.append(f"backup.pairs darf höchstens {_MAX_PAIRS} Einträge enthalten")
        pairs = pairs[:_MAX_PAIRS]
    # Vor dem Entfernen der Legacy-Zeitpläne ableiten. Der Helper nutzt dieselbe
    # deterministische Datenweg-ID wie die Normalisierung weiter unten.
    migrated_legacy_jobs = legacy_job_definitions({**backup, "pairs": pairs})
    normalized_pairs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_pair_ids: set[str] = set()
    for idx, raw in enumerate(pairs):
        label = f"backup.pairs[{idx}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} muss ein Mapping sein")
            continue
        pair = copy.deepcopy(raw)
        name = str(pair.get("name") or "").strip()
        if not _NAME_RE.match(name):
            errors.append(f"{label}.name fehlt oder ist ungültig")
        elif "," in name or name.casefold().startswith("pbs:"):
            errors.append(
                f"{label}.name darf weder Kommas enthalten noch mit 'pbs:' beginnen"
            )
        folded = name.casefold()
        if folded in seen_names:
            errors.append(f"Doppelter Pair-Name: {name}")
        seen_names.add(folded)
        pair["name"] = name
        pair["enabled"] = _boolean(pair.get("enabled", True), default=True)

        legacy_options = pair.pop("options", None)
        if legacy_options not in (None, {}):
            errors.append(
                f"{label}.options wird nicht mehr unterstützt; "
                "Werte müssen als explizite Pair-Felder konfiguriert werden"
            )

        try:
            remote = _clean_path(pair.get("remote"))
            local = _clean_path(pair.get("local"))
        except ValueError as exc:
            errors.append(f"{label} Pfad {exc}")
            remote = local = ""
        if not _is_remote(remote) and not _is_absolute_local(remote):
            errors.append(
                f"{label}.remote muss ein rclone-Pfad (remote:/ordner) "
                "oder ein absoluter lokaler Pfad sein"
            )
        if not local:
            errors.append(f"{label}.local fehlt")
        elif not _is_remote(local) and not _is_absolute_local(local):
            errors.append(f"{label}.local muss absolut sein")
        if remote and local and remote.rstrip("/") == local.rstrip("/"):
            errors.append(f"{label}: Quelle und Ziel sind identisch")
        elif (
            _is_absolute_local(remote)
            and _is_absolute_local(local)
            and _paths_overlap(remote, local)
        ):
            errors.append(
                f"{label}: Lokale Pfade dürfen nicht ineinander liegen (Endlos-Kopien)"
            )
        pair["remote"] = remote
        pair["local"] = local

        direction = str(pair.get("direction") or "bisync").lower().strip()
        if direction not in {"bisync", "pull", "push"}:
            errors.append(f"{label}.direction ist ungültig")
            direction = "bisync"
        mode = (
            str(pair.get("mode") or ("bisync" if direction == "bisync" else "copy"))
            .lower()
            .strip()
        )
        if direction == "bisync":
            mode = "bisync"
        elif mode not in {"copy", "sync"}:
            errors.append(f"{label}.mode ist ungültig")
            mode = "copy"
        pair["direction"] = direction
        pair["mode"] = mode

        # Versionsablage: --backup-dir bewahrt überschriebene und gelöschte
        # Dateien auf. Ohne sie repliziert ein sync/bisync eine Verschlüsselung
        # oder Massenlöschung an der Quelle binnen eines Laufs zum Ziel — der
        # Löschschutz greift dabei nicht, weil Dateien ersetzt statt gelöscht
        # werden.
        destructive = mode == "sync" or direction == "bisync"
        for key in ("backup_dir", "backup_dir1", "backup_dir2"):
            spec = str(pair.get(key) or "").strip()
            if not spec:
                pair[key] = ""
                continue
            if any(ch in spec for ch in ("\x00", "\r", "\n")):
                errors.append(f"{label}.{key} enthält unzulässige Zeichen")
                spec = ""
            elif ".." in Path(spec.replace("{date}", "x")).parts:
                errors.append(f"{label}.{key} darf kein '..' enthalten")
                spec = ""
            else:
                # Nur absolute bzw. Remote-Angaben sind hier prüfbar; relative
                # werden erst zur Laufzeit an die Zielwurzel gehängt.
                probe = spec.replace("{date}", "0000-00-00T00-00-00")
                if _is_remote(probe) or _is_absolute_local(probe):
                    for endpoint, endpoint_label in (
                        (remote, "remote"),
                        (local, "local"),
                    ):
                        if endpoint and _paths_overlap(probe, endpoint):
                            errors.append(
                                f"{label}.{key} darf nicht mit {endpoint_label} "
                                "überlappen — rclone bricht sonst ab"
                            )
                            break
                elif destructive:
                    warnings.append(
                        f"{name}: {key} ist relativ und landet damit im Ziel selbst. "
                        "rclone lehnt überlappende Verzeichnisse ab — einen Pfad "
                        "neben dem Ziel angeben, z. B. "
                        f"{(remote or 'ziel').rstrip('/')}-versions/{{date}}."
                    )
            pair[key] = spec
        if (
            destructive
            and pair["enabled"]
            and not any(
                pair.get(key) for key in ("backup_dir", "backup_dir1", "backup_dir2")
            )
        ):
            warnings.append(
                f"{name}: {'Bi-Sync' if direction == 'bisync' else 'Mirror (sync)'} "
                "ohne backup_dir — überschriebene und gelöschte Dateien sind sofort "
                "unwiederbringlich. Eine Versionsablage neben dem Ziel setzen."
            )

        pair_id = str(pair.get("id") or "").strip().lower()
        if not pair_id:
            identity = "\0".join(
                ("rclone", name.casefold(), remote, local, direction, mode)
            )
            pair_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        elif not _STABLE_ID_RE.fullmatch(pair_id):
            errors.append(f"{label}.id ist ungültig")
        if pair_id in seen_pair_ids:
            errors.append(f"{label}.id ist doppelt")
        seen_pair_ids.add(pair_id)
        pair["id"] = pair_id

        schedule = str(pair.get("schedule") or "").strip()
        if (
            schedule
            and schedule.lower() not in _DISABLED_SCHEDULES
            and not _valid_cron(schedule)
        ):
            errors.append(f"{label}.schedule ist ungültig: {schedule}")
        # Zeitplanung gehört ausschließlich in backup.jobs.
        pair.pop("schedule", None)
        pair["min_local_files"] = int(
            _number(
                pair.get("min_local_files", 1),
                default=1,
                minimum=0,
                maximum=10_000_000,
                integer=True,
            )
        )
        pair["min_remote_files"] = int(
            _number(
                pair.get("min_remote_files", 0),
                default=0,
                minimum=0,
                maximum=10_000_000,
                integer=True,
            )
        )
        if (
            pair["enabled"]
            and hidden_remote_paths
            and is_hidden_remote_path(remote, hidden_remote_paths)
        ):
            warnings.append(
                f"{name}: Ziel liegt unter einem per web.hidden_remote_paths "
                "ausgeblendeten Remote-Pfad und ist im Browser nicht auswählbar."
            )
        pair["allow_empty_remote_target"] = _boolean(
            pair.get("allow_empty_remote_target", False)
        )
        if (
            pair["enabled"]
            and _is_absolute_local(remote)
            and direction in {"push", "bisync"}
            and pair["min_remote_files"] == 0
        ):
            if pair["allow_empty_remote_target"]:
                warnings.append(
                    f"{name}: lokales Ziel im remote-Feld ist per "
                    "allow_empty_remote_target ausdrücklich ohne Mount-Drop-Schutz."
                )
            else:
                pair["min_remote_files"] = 1
                warnings.append(
                    f"{name}: lokales Ziel im remote-Feld — min_remote_files auf 1 "
                    "gesetzt (Mount-Drop-Schutz). Für ein bewusst leeres Ziel "
                    "allow_empty_remote_target aktivieren."
                )
        pair["min_free_gb"] = float(
            _number(pair.get("min_free_gb", 0), default=0, minimum=0, maximum=1_000_000)
        )
        pair["max_success_age_hours"] = float(
            _number(
                pair.get("max_success_age_hours", 0),
                default=0,
                minimum=0,
                maximum=8760,
            )
        )
        pair["require_mountpoint"] = _boolean(pair.get("require_mountpoint", False))
        mountpoint = str(pair.get("mountpoint") or "").strip()
        if mountpoint:
            try:
                mountpoint = _clean_path(mountpoint)
                if not _is_absolute_local(mountpoint):
                    errors.append(f"{label}.mountpoint muss absolut sein")
                elif local and not _is_remote(local):
                    try:
                        local_resolved = Path(local).expanduser().resolve()
                        mount_resolved = Path(mountpoint).expanduser().resolve()
                        if (
                            local_resolved != mount_resolved
                            and not local_resolved.is_relative_to(mount_resolved)
                        ):
                            errors.append(
                                f"{label}.mountpoint muss den lokalen Pfad enthalten"
                            )
                    except (OSError, RuntimeError, ValueError) as exc:
                        errors.append(f"{label}.mountpoint ist nicht prüfbar: {exc}")
            except ValueError as exc:
                errors.append(f"{label}.mountpoint {exc}")
        pair["mountpoint"] = mountpoint
        pair["allow_delete"] = _boolean(pair.get("allow_delete", False))
        sentinel = str(pair.get("sentinel_file") or "").strip()
        if sentinel:
            sentinel_path = Path(sentinel)
            if (
                _is_absolute_local(str(sentinel_path))
                or ".." in sentinel_path.parts
                or any(ch in sentinel for ch in ("\x00", "\r", "\n"))
            ):
                errors.append(
                    f"{label}.sentinel_file muss ein sicherer relativer Pfad sein"
                )
        pair["sentinel_file"] = sentinel
        if pair.get("max_delete") not in (None, ""):
            pair["max_delete"] = int(
                _number(
                    pair.get("max_delete"),
                    default=100,
                    minimum=-1,
                    maximum=10_000_000,
                    integer=True,
                )
            )
        for unsafe_key in ("ignore_errors", "allow_unsafe_flags"):
            if _boolean(pair.pop(unsafe_key, False)):
                errors.append(
                    f"{label}.{unsafe_key} ist nicht als strukturierte Pair-Option erlaubt"
                )
        for key, low, high in (
            ("transfers", 1, 128),
            ("checkers", 1, 256),
            ("retries", 0, 100),
            ("low_level_retries", 0, 1000),
            ("tpslimit", 0, 1_000_000),
            ("tpslimit_burst", 0, 1_000_000),
        ):
            if pair.get(key) not in (None, ""):
                pair[key] = int(
                    _number(
                        pair.get(key),
                        default=low,
                        minimum=low,
                        maximum=high,
                        integer=True,
                    )
                )
        for key in (
            "delete_excluded",
            "fast_list",
            "track_renames",
            "metadata",
            "create_empty_src_dirs",
            "ignore_existing",
            "drive_acknowledge_abuse",
        ):
            if key in pair:
                pair[key] = _boolean(pair.get(key))
        for key in (
            "max_transfer",
            "max_duration",
            "contimeout",
            "timeout",
            "bwlimit",
            "max_delete_size",
        ):
            if key in pair:
                value = str(pair.get(key) or "").strip()
                if len(value) > 64 or any(ch in value for ch in "\x00\r\n"):
                    errors.append(f"{label}.{key} ist ungültig")
                pair[key] = value
        if "log_level" in pair:
            log_level = str(pair.get("log_level") or "").strip().upper()
            if log_level not in {"DEBUG", "INFO", "NOTICE", "ERROR"}:
                errors.append(f"{label}.log_level ist ungültig")
            pair["log_level"] = log_level
        try:
            pair["rclone_args"] = _normalize_string_list(pair.get("rclone_args"))
            validate_rclone_args(
                pair["rclone_args"], allow_unsafe=backup["allow_unsafe_rclone_args"]
            )
        except (ValueError, UnsafeRcloneArgument) as exc:
            errors.append(f"{label}.rclone_args {exc}")

        for file_key in ("include_file", "exclude_file", "filter_file"):
            file_value = str(pair.get(file_key) or "").strip()
            if file_value:
                try:
                    clean_file_value = _clean_path(file_value)
                    candidate = Path(clean_file_value).expanduser()
                    if not _is_absolute_local(clean_file_value):
                        errors.append(f"{label}.{file_key} muss absolut sein")
                    elif not backup["allow_external_filter_files"]:
                        data_root = (
                            Path(str(paths.get("data_dir") or "/opt/rclone-sync/data"))
                            .expanduser()
                            .resolve()
                        )
                        resolved = candidate.resolve()
                        if resolved != data_root and not resolved.is_relative_to(
                            data_root
                        ):
                            errors.append(
                                f"{label}.{file_key} muss innerhalb paths.data_dir liegen"
                            )
                except (ValueError, OSError, RuntimeError) as exc:
                    errors.append(f"{label}.{file_key} ist ungültig: {exc}")
            pair[file_key] = file_value

        destructive = direction == "bisync" or (
            direction in {"pull", "push"} and mode == "sync"
        )
        if destructive and pair["enabled"]:
            if backup["require_delete_confirmation"] and not pair["allow_delete"]:
                warnings.append(
                    f"{name}: produktiver {mode} bleibt gesperrt, bis allow_delete aktiviert ist."
                )
            effective_max_delete = pair.get(
                "max_delete", (backup.get("tuning") or {}).get("max_delete")
            )
            if backup["require_max_delete_for_sync"] and effective_max_delete in (
                None,
                "",
                -1,
                "-1",
            ):
                warnings.append(
                    f"{name}: produktiver {mode} bleibt ohne begrenztes max_delete gesperrt."
                )
        for text_key in ("include", "exclude", "filter"):
            value = pair.get(text_key, "")
            if isinstance(value, list):
                value = "\n".join(str(item) for item in value)
            value = str(value or "")
            if len(value.encode("utf-8")) > 512 * 1024:
                errors.append(f"{label}.{text_key} ist zu groß")
            pair[text_key] = value
        normalized_pairs.append(pair)

    backup["pairs"] = normalized_pairs

    raw_jobs = backup.get("jobs") if jobs_were_explicit else migrated_legacy_jobs
    if not isinstance(raw_jobs, list):
        errors.append("backup.jobs muss eine Liste sein")
        raw_jobs = []
    elif len(raw_jobs) > _MAX_JOBS:
        errors.append(f"backup.jobs darf höchstens {_MAX_JOBS} Einträge enthalten")
        raw_jobs = raw_jobs[:_MAX_JOBS]

    normalized_jobs: list[dict[str, Any]] = []
    seen_job_ids: set[str] = set()
    seen_job_names: set[str] = set()
    known_path_ids = {str(pair.get("id") or "") for pair in normalized_pairs}
    for index, raw_job in enumerate(raw_jobs):
        label = f"backup.jobs[{index}]"
        if not isinstance(raw_job, dict):
            errors.append(f"{label} muss ein Mapping sein")
            continue
        job = copy.deepcopy(raw_job)
        name = str(job.get("name") or "").strip()
        if not _NAME_RE.match(name):
            errors.append(f"{label}.name fehlt oder ist ungültig")
        folded_name = name.casefold()
        if folded_name in seen_job_names:
            errors.append(f"Doppelter Job-Name: {name}")
        seen_job_names.add(folded_name)

        raw_path_ids = job.get("data_path_ids")
        if not isinstance(raw_path_ids, list):
            errors.append(f"{label}.data_path_ids muss eine Liste sein")
            raw_path_ids = []
        path_ids = [str(value or "").strip().lower() for value in raw_path_ids]
        if not path_ids:
            errors.append(f"{label} muss mindestens einen Datenweg referenzieren")
        if len(set(path_ids)) != len(path_ids):
            errors.append(f"{label}.data_path_ids enthält Duplikate")
        unknown = [path_id for path_id in path_ids if path_id not in known_path_ids]
        if unknown:
            errors.append(
                f"{label}.data_path_ids enthält unbekannte IDs: {', '.join(unknown)}"
            )

        job_id = str(job.get("id") or "").strip().lower()
        if not job_id:
            job_id = stable_job_id(name, path_ids)
        elif not _STABLE_ID_RE.fullmatch(job_id):
            errors.append(f"{label}.id ist ungültig")
        if job_id in seen_job_ids:
            errors.append(f"{label}.id ist doppelt")
        seen_job_ids.add(job_id)

        schedule = str(job.get("schedule") or "manual").strip() or "manual"
        if schedule.casefold() not in _DISABLED_SCHEDULES and not _valid_cron(schedule):
            errors.append(f"{label}.schedule ist ungültig: {schedule}")
        execution_mode = str(job.get("execution_mode") or "sequential").casefold()
        if execution_mode not in {"sequential", "parallel"}:
            errors.append(f"{label}.execution_mode ist ungültig")
            execution_mode = "sequential"
        max_parallel = int(
            _number(
                job.get("max_parallel", 1),
                default=1,
                minimum=1,
                maximum=16,
                integer=True,
            )
        )
        if execution_mode == "sequential":
            max_parallel = 1
        retry_minutes = int(
            _number(
                job.get("retry_minutes", backup["scheduler_retry_minutes"]),
                default=backup["scheduler_retry_minutes"],
                minimum=1,
                maximum=10080,
                integer=True,
            )
        )
        normalized_jobs.append(
            {
                "id": job_id,
                "name": name,
                "enabled": _boolean(job.get("enabled", True), default=True),
                "data_path_ids": path_ids,
                "schedule": schedule,
                "execution_mode": execution_mode,
                "max_parallel": max_parallel,
                "retry_minutes": retry_minutes,
            }
        )
    backup["jobs"] = normalized_jobs

    for i, first in enumerate(normalized_pairs):
        for second in normalized_pairs[i + 1 :]:
            overlap_keys = next(
                (
                    (first_key, second_key)
                    for first_key in ("local", "remote")
                    for second_key in ("local", "remote")
                    if _paths_overlap(
                        str(first.get(first_key) or ""),
                        str(second.get(second_key) or ""),
                    )
                ),
                None,
            )
            if overlap_keys:
                first_key, second_key = overlap_keys
                warnings.append(
                    f"Pairs '{first.get('name')}' und '{second.get('name')}' "
                    f"überlappen bei {first_key}/{second_key}; "
                    "sie werden zur Sicherheit seriell ausgeführt."
                )

    notifications = cfg.setdefault("notifications", {})
    if not isinstance(notifications, dict):
        errors.append("notifications muss ein Mapping sein")
        notifications = cfg["notifications"] = {}
    notifications["allow_http"] = _boolean(notifications.get("allow_http", False))
    notifications["allow_private_targets"] = _boolean(
        notifications.get("allow_private_targets", False)
    )
    hooks = notifications.get("webhooks") or []
    if not isinstance(hooks, list):
        errors.append("notifications.webhooks muss eine Liste sein")
        hooks = []
    elif len(hooks) > _MAX_WEBHOOKS:
        errors.append(
            f"notifications.webhooks darf höchstens {_MAX_WEBHOOKS} Einträge enthalten"
        )
        hooks = hooks[:_MAX_WEBHOOKS]
    seen_hook_ids: set[str] = set()
    normalized_hooks: list[dict[str, Any]] = []
    for idx, hook in enumerate(hooks):
        if not isinstance(hook, dict):
            errors.append(f"notifications.webhooks[{idx}] muss ein Mapping sein")
            continue
        hook_id = str(hook.get("id") or "").strip()
        if not _WEBHOOK_ID_RE.match(hook_id) or hook_id in seen_hook_ids:
            hook_id = uuid.uuid4().hex
        seen_hook_ids.add(hook_id)
        hook["id"] = hook_id
        hook["enabled"] = _boolean(hook.get("enabled", True), default=True)
        url = str(hook.get("url") or "").strip()
        if url:
            parsed = urlsplit(url.replace("{message}", "message"))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                errors.append(f"notifications.webhooks[{idx}].url ist ungültig")
            elif parsed.scheme == "http" and not notifications["allow_http"]:
                errors.append(f"notifications.webhooks[{idx}].url muss HTTPS verwenden")
            if parsed.username or parsed.password:
                errors.append(
                    f"notifications.webhooks[{idx}].url darf keine Zugangsdaten im Hostteil enthalten"
                )
        hook["url"] = url
        kind = str(hook.get("type") or "generic").lower()
        if kind not in {"discord", "telegram", "generic"}:
            errors.append(f"notifications.webhooks[{idx}].type ist ungültig")
        hook["type"] = kind
        events = hook.get("events") or []
        if not isinstance(events, list):
            errors.append(f"notifications.webhooks[{idx}].events muss eine Liste sein")
        else:
            normalized_events = list(
                dict.fromkeys(str(event) for event in events if str(event))
            )
            invalid_events = [
                event
                for event in normalized_events
                if event not in _NOTIFICATION_EVENTS
            ]
            if invalid_events:
                errors.append(
                    f"notifications.webhooks[{idx}].events enthält unbekannte Events: {', '.join(invalid_events)}"
                )
            hook["events"] = [
                event for event in normalized_events if event in _NOTIFICATION_EVENTS
            ]
        normalized_hooks.append(hook)
    notifications["webhooks"] = normalized_hooks
    notifications["timeout_seconds"] = float(
        _number(
            notifications.get("timeout_seconds", 10), default=10, minimum=1, maximum=60
        )
    )
    notifications["max_parallel"] = int(
        _number(
            notifications.get("max_parallel", 4),
            default=4,
            minimum=1,
            maximum=16,
            integer=True,
        )
    )

    maintenance = cfg.setdefault("maintenance", {})
    if not isinstance(maintenance, dict):
        errors.append("maintenance muss ein Mapping sein")
        maintenance = cfg["maintenance"] = {}
    maintenance["auto_prune"] = _boolean(
        maintenance.get("auto_prune", True), default=True
    )
    maintenance["job_retention_days"] = int(
        _number(
            maintenance.get("job_retention_days", 180),
            default=180,
            minimum=1,
            maximum=3650,
            integer=True,
        )
    )
    maintenance["keep_latest_jobs"] = int(
        _number(
            maintenance.get("keep_latest_jobs", 500),
            default=500,
            minimum=10,
            maximum=100000,
            integer=True,
        )
    )
    maintenance["log_retention_days"] = int(
        _number(
            maintenance.get("log_retention_days", 90),
            default=90,
            minimum=1,
            maximum=3650,
            integer=True,
        )
    )

    pbs = cfg.setdefault("pbs", {})
    if not isinstance(pbs, dict):
        errors.append("pbs muss ein Mapping sein")
        pbs = cfg["pbs"] = {}
    pbs["enabled"] = _boolean(pbs.get("enabled", False))
    repository = str(pbs.get("repository") or "").strip()
    if pbs["enabled"] and not repository:
        errors.append("pbs.repository ist erforderlich, wenn pbs.enabled gesetzt ist")
    if repository and not re.match(
        r"^[^\s@]+@[^\s@!]+(?:![^\s@:]+)?@[^\s:]+:\S+$", repository
    ):
        warnings.append(
            "pbs.repository sieht ungewöhnlich aus — erwartet user@realm[!token]@host:datastore"
        )
    pbs["repository"] = repository
    pbs["namespace"] = str(pbs.get("namespace") or "").strip()
    pbs["backup_id"] = str(pbs.get("backup_id") or "").strip()
    if pbs["backup_id"] and not _PBS_BACKUP_ID_RE.fullmatch(pbs["backup_id"]):
        errors.append(
            "pbs.backup_id muss mit einem Buchstaben oder einer Ziffer beginnen "
            "und darf nur Buchstaben, Ziffern, Punkt, Unterstrich und Bindestrich "
            "enthalten (maximal 80 Zeichen)"
        )
    fingerprint = str(pbs.get("fingerprint") or "").strip()
    if fingerprint and not re.match(
        r"^[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){31}$", fingerprint
    ):
        errors.append("pbs.fingerprint muss ein SHA-256-Fingerprint (aa:bb:…) sein")
    pbs["fingerprint"] = fingerprint
    pbs["password"] = str(pbs.get("password") or "")
    if pbs["enabled"] and not pbs["password"]:
        warnings.append(
            "pbs.password ist leer — proxmox-backup-client wird ohne Token/Passwort scheitern"
        )
    pbs["timeout_hours"] = _number(
        pbs.get("timeout_hours", 4), default=4, minimum=0.1, maximum=168
    )
    keep = pbs.setdefault("keep", {})
    if not isinstance(keep, dict):
        errors.append("pbs.keep muss ein Mapping sein")
        keep = pbs["keep"] = {}
    for key in (
        "keep_last",
        "keep_daily",
        "keep_weekly",
        "keep_monthly",
        "keep_yearly",
    ):
        if keep.get(key) in (None, ""):
            keep[key] = 0
            continue
        keep[key] = int(
            _number(keep.get(key), default=0, minimum=0, maximum=3650, integer=True)
        )
    targets = pbs.setdefault("targets", [])
    if not isinstance(targets, list):
        errors.append("pbs.targets muss eine Liste sein")
        targets = pbs["targets"] = []
    elif len(targets) > _MAX_PBS_TARGETS:
        errors.append(
            f"pbs.targets darf höchstens {_MAX_PBS_TARGETS} Einträge enthalten"
        )
        targets = pbs["targets"] = targets[:_MAX_PBS_TARGETS]
    seen_targets: set[str] = set()
    seen_target_ids: set[str] = set()
    for index, target in enumerate(targets):
        label = f"pbs.targets[{index}]"
        if not isinstance(target, dict):
            errors.append(f"{label} muss ein Mapping sein")
            continue
        name = str(target.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            errors.append(f"{label}.name ist ungültig")
        elif "," in name:
            errors.append(f"{label}.name darf kein Komma enthalten")
        folded_name = name.casefold()
        if folded_name in seen_targets:
            errors.append(f"{label}.name ist doppelt: {name}")
        seen_targets.add(folded_name)
        target["name"] = name
        try:
            paths = _normalize_string_list(target.get("paths"))
        except ValueError:
            errors.append(f"{label}.paths muss Text oder Liste sein")
            paths = []
        if not paths:
            errors.append(f"{label}.paths braucht mindestens einen Pfad")
        elif len(paths) > _MAX_PBS_PATHS:
            errors.append(
                f"{label}.paths darf höchstens {_MAX_PBS_PATHS} Pfade enthalten"
            )
            paths = paths[:_MAX_PBS_PATHS]
        clean_paths: list[str] = []
        for path_value in paths:
            try:
                path_value = _clean_path(path_value)
            except ValueError as exc:
                errors.append(f"{label}: Pfad {exc}")
                continue
            if not _is_absolute_local(path_value):
                errors.append(f"{label}: Pfad muss absolut sein: {path_value}")
            clean_paths.append(path_value)
        paths = clean_paths
        target["paths"] = paths
        target["namespace"] = str(target.get("namespace") or "").strip()
        target["backup_id"] = str(target.get("backup_id") or "").strip()
        if target["backup_id"] and not _PBS_BACKUP_ID_RE.fullmatch(target["backup_id"]):
            errors.append(
                f"{label}.backup_id muss mit einem Buchstaben oder einer Ziffer "
                "beginnen und darf nur Buchstaben, Ziffern, Punkt, Unterstrich "
                "und Bindestrich enthalten (maximal 80 Zeichen)"
            )
        target_id = str(target.get("id") or "").strip().lower()
        if not target_id:
            identity = "\0".join(
                (
                    "pbs",
                    repository,
                    str(target["namespace"] or pbs.get("namespace") or "").strip(),
                    str(target["backup_id"] or pbs.get("backup_id") or "").strip(),
                    *sorted(paths),
                )
            )
            target_id = uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
        elif not _STABLE_ID_RE.fullmatch(target_id):
            errors.append(f"{label}.id ist ungültig")
        if target_id in seen_target_ids:
            errors.append(f"{label}.id ist doppelt")
        seen_target_ids.add(target_id)
        target["id"] = target_id
        schedule = str(target.get("schedule") or "manual").strip()
        if schedule.lower() not in _DISABLED_SCHEDULES and not _valid_cron(schedule):
            errors.append(f"{label}.schedule ist ungültig: {schedule}")
        target["schedule"] = schedule
        target["require_mountpoint"] = _boolean(target.get("require_mountpoint", False))
        mountpoint = str(target.get("mountpoint") or "").strip()
        if mountpoint:
            try:
                mountpoint = _clean_path(mountpoint)
                if not _is_absolute_local(mountpoint):
                    errors.append(f"{label}.mountpoint muss absolut sein")
                else:
                    mount_resolved = Path(mountpoint).expanduser().resolve()
                    for path_value in paths:
                        if not _is_absolute_local(path_value):
                            continue
                        path_resolved = Path(path_value).expanduser().resolve()
                        if (
                            path_resolved != mount_resolved
                            and not path_resolved.is_relative_to(mount_resolved)
                        ):
                            errors.append(
                                f"{label}.mountpoint muss alle PBS-Pfade enthalten: "
                                f"{path_value}"
                            )
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{label}.mountpoint ist nicht prüfbar: {exc}")
        target["mountpoint"] = mountpoint
        sentinel = str(target.get("sentinel_file") or "").strip()
        sentinel_path = Path(sentinel)
        if sentinel and (
            _is_absolute_local(sentinel)
            or ".." in sentinel_path.parts
            or any(ch in sentinel for ch in "\x00\r\n")
        ):
            errors.append(
                f"{label}.sentinel_file muss ein sicherer relativer Pfad sein"
            )
        target["sentinel_file"] = sentinel
        target["min_files"] = int(
            _number(
                target.get("min_files", 1),
                default=1,
                minimum=0,
                maximum=10_000_000,
                integer=True,
            )
        )
    if len(targets) > 1:
        seen_backup_ids: set[str] = set()
        for index, target in enumerate(targets):
            if not isinstance(target, dict):
                continue
            label = f"pbs.targets[{index}]"
            backup_id = str(target.get("backup_id") or "").strip()
            if not backup_id:
                errors.append(
                    f"{label}.backup_id ist bei mehreren PBS-Targets explizit "
                    "erforderlich"
                )
                continue
            folded_backup_id = backup_id.casefold()
            if folded_backup_id in seen_backup_ids:
                errors.append(
                    f"{label}.backup_id ist bei mehreren PBS-Targets nicht eindeutig: "
                    f"{backup_id}"
                )
            seen_backup_ids.add(folded_backup_id)
    if pbs["enabled"] and not targets:
        warnings.append("pbs.enabled ohne pbs.targets — es wird nichts gesichert")

    # Unbekannte Keys sind kein Fehler — sonst würde jede ältere oder von Hand
    # erweiterte Config beim Laden scheitern. Ein Tippfehler in einem
    # Sicherheitsschalter wäre aber sonst wirkungslos und unsichtbar.
    for key in sorted(k for k in cfg if isinstance(k, str)):
        if key not in _KNOWN_SECTIONS:
            warnings.append(
                f"Unbekannte Konfigurationssektion {key!r} — wird ignoriert, "
                "bitte auf Tippfehler prüfen."
            )
    for key in sorted(k for k in web if isinstance(k, str)):
        if key not in _KNOWN_WEB_KEYS:
            warnings.append(
                f"Unbekannter Schlüssel web.{key} — wird ignoriert, bitte auf "
                "Tippfehler prüfen."
            )

    if errors:
        raise ConfigValidationError(errors)
    return cfg, list(dict.fromkeys(warnings))
