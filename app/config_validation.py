"""Validierung und Normalisierung der editierbaren YAML-Konfiguration."""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from croniter import croniter

from .rclone_args import UnsafeRcloneArgument, validate_rclone_args

_DISABLED_SCHEDULES = {"", "off", "manual", "disabled", "none"}
_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,80}$")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(?:ms|s|m|h|d|w)?$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,127}:.*$")
_WEBHOOK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_NOTIFICATION_EVENTS = {
    "sync_started",
    "sync_ok",
    "sync_error",
    "conflict",
    "mount_check_failed",
    "cancelled",
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
    return bool(path and _REMOTE_RE.match(path) and not path.startswith("-"))


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


def validate_config(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(data, dict):
        raise ConfigValidationError(["Config-Root muss ein Mapping sein"])

    cfg = copy.deepcopy(data)
    errors: list[str] = []
    warnings: list[str] = []

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
            if not root.startswith("/"):
                errors.append(f"Browser-Root muss absolut sein: {root}")
            else:
                clean_roots.append(root.rstrip("/") or "/")
        except ValueError as exc:
            errors.append(f"Browser-Root {exc}")
    web["local_browse_roots"] = list(dict.fromkeys(clean_roots))

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
            if not value.startswith("/"):
                errors.append(f"paths.{key} muss absolut sein")
            paths[key] = value
        except ValueError as exc:
            errors.append(f"paths.{key} {exc}")

    backup = cfg.setdefault("backup", {})
    if not isinstance(backup, dict):
        errors.append("backup muss ein Mapping sein")
        backup = cfg["backup"] = {}
    backup["enabled"] = _boolean(backup.get("enabled", True), default=True)
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
    timezone = str(backup.get("timezone") or "Europe/Berlin").strip()
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone)
    except Exception:
        errors.append(f"backup.timezone ist ungültig: {timezone}")
    backup["timezone"] = timezone

    default_schedule = str(backup.get("default_schedule") or "manual").strip()
    if default_schedule.lower() not in _DISABLED_SCHEDULES and not croniter.is_valid(
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
            filter_path = Path(_clean_path(filter_file)).expanduser()
            if not filter_path.is_absolute():
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
    normalized_pairs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for idx, raw in enumerate(pairs):
        label = f"backup.pairs[{idx}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} muss ein Mapping sein")
            continue
        pair = copy.deepcopy(raw)
        name = str(pair.get("name") or "").strip()
        if not _NAME_RE.match(name):
            errors.append(f"{label}.name fehlt oder ist ungültig")
        folded = name.casefold()
        if folded in seen_names:
            errors.append(f"Doppelter Pair-Name: {name}")
        seen_names.add(folded)
        pair["name"] = name
        pair["enabled"] = _boolean(pair.get("enabled", True), default=True)

        try:
            remote = _clean_path(pair.get("remote"))
            local = _clean_path(pair.get("local"))
        except ValueError as exc:
            errors.append(f"{label} Pfad {exc}")
            remote = local = ""
        if not _is_remote(remote):
            errors.append(
                f"{label}.remote muss ein rclone-Pfad wie remote:/ordner sein"
            )
        if not local:
            errors.append(f"{label}.local fehlt")
        elif not _is_remote(local) and not local.startswith("/"):
            errors.append(f"{label}.local muss absolut sein")
        if remote and local and remote.rstrip("/") == local.rstrip("/"):
            errors.append(f"{label}: Quelle und Ziel sind identisch")
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

        schedule = str(pair.get("schedule") or "").strip()
        if (
            schedule
            and schedule.lower() not in _DISABLED_SCHEDULES
            and not croniter.is_valid(schedule)
        ):
            errors.append(f"{label}.schedule ist ungültig: {schedule}")
        pair["schedule"] = schedule
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
                if not mountpoint.startswith("/"):
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
                sentinel_path.is_absolute()
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
                    candidate = Path(_clean_path(file_value)).expanduser()
                    if not candidate.is_absolute():
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

    for i, first in enumerate(normalized_pairs):
        for second in normalized_pairs[i + 1 :]:
            for key in ("local", "remote"):
                if _paths_overlap(
                    str(first.get(key) or ""), str(second.get(key) or "")
                ):
                    warnings.append(
                        f"Pairs '{first.get('name')}' und '{second.get('name')}' überlappen bei {key}; "
                        "sie werden zur Sicherheit seriell ausgeführt."
                    )
                    break

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

    if errors:
        raise ConfigValidationError(errors)
    return cfg, list(dict.fromkeys(warnings))
