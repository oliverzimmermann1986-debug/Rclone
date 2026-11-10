"""Cloud-Quota (``rclone about``), Wachstumsprognose und monatliches Transfer-Budget."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import bandwidth
from .rclone_args import rclone_subprocess_env
from .recovery_points import _is_remote

DEFAULT_BUDGET: dict[str, Any] = {
    "enabled": False,
    "monthly_egress_gb": 0,
    "monthly_upload_gb": 0,
    "warn_percent": 80,
    "throttle_optional_jobs": True,
}
CACHE_TTL_SECONDS = 900
GIB = 1024**3

_cache_lock = threading.Lock()
_about_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _as_mapping(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    snapshot = getattr(config, "snapshot", None)
    if callable(snapshot):
        data = snapshot()
        if isinstance(data, Mapping):
            return data
    raw = getattr(config, "_data", None)
    if isinstance(raw, Mapping):
        return raw
    return {}


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _configured_timezone(config: Mapping[str, Any]) -> ZoneInfo:
    cfg = _as_mapping(config)
    backup = cfg.get("backup") if isinstance(cfg.get("backup"), Mapping) else {}
    name = str((backup or {}).get("timezone") or "Europe/Berlin")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _extract_remote_name(path: Any) -> str | None:
    raw = str(path or "").strip()
    if not raw or not _is_remote(raw):
        return None
    remote = raw.split(":", 1)[0].strip()
    if (
        not remote
        or remote.startswith("-")
        or any(ch in remote for ch in ("/", "\\", "\x00", "\r", "\n"))
    ):
        return None
    return remote


def _extract_transferred_bytes(result: Mapping[str, Any]) -> int:
    transferred = result.get("transferred")
    if isinstance(transferred, (int, float)) and not isinstance(transferred, bool):
        return max(0, int(transferred))
    if isinstance(transferred, str):
        head = transferred.split("/", 1)[0].split(",", 1)[0].strip()
        parsed = bandwidth.parse_size(head)
        if parsed is not None:
            return max(0, int(parsed))
    fallback = result.get("bytes_transferred")
    if isinstance(fallback, (int, float)) and not isinstance(fallback, bool):
        return max(0, int(fallback))
    if isinstance(fallback, str):
        try:
            return max(0, int(fallback.strip()))
        except ValueError:
            parsed = bandwidth.parse_size(fallback.strip())
            if parsed is not None:
                return max(0, int(parsed))
    return 0


def budget_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = _as_mapping(config)
    backup = cfg.get("backup") if isinstance(cfg.get("backup"), Mapping) else {}
    raw = (
        backup.get("transfer_budget")
        if isinstance((backup or {}).get("transfer_budget"), Mapping)
        else {}
    )
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_BUDGET["enabled"])),
        "monthly_egress_gb": _clamp_int(
            raw.get("monthly_egress_gb"),
            DEFAULT_BUDGET["monthly_egress_gb"],
            0,
            1_000_000,
        ),
        "monthly_upload_gb": _clamp_int(
            raw.get("monthly_upload_gb"),
            DEFAULT_BUDGET["monthly_upload_gb"],
            0,
            1_000_000,
        ),
        "warn_percent": _clamp_int(
            raw.get("warn_percent"),
            DEFAULT_BUDGET["warn_percent"],
            10,
            100,
        ),
        "throttle_optional_jobs": bool(
            raw.get(
                "throttle_optional_jobs",
                DEFAULT_BUDGET["throttle_optional_jobs"],
            )
        ),
    }


def configured_remotes(config: Mapping[str, Any]) -> list[str]:
    cfg = _as_mapping(config)
    backup = cfg.get("backup") if isinstance(cfg.get("backup"), Mapping) else {}
    pairs = (backup or {}).get("pairs") or []
    seen: list[str] = []
    if not isinstance(pairs, list):
        return seen
    for pair in pairs:
        if not isinstance(pair, Mapping):
            continue
        for field in ("remote", "local"):
            remote_name = _extract_remote_name(pair.get(field))
            if remote_name and remote_name not in seen:
                seen.append(remote_name)
    return seen


def remote_about(
    remote_name: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    timeout: int = 30,
) -> dict[str, Any]:
    clean_name = str(remote_name or "").strip().rstrip(":")
    measured_at = time.time()
    base: dict[str, Any] = {
        "remote": clean_name,
        "ok": False,
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "trashed_bytes": None,
        "other_bytes": None,
        "percent_used": None,
        "error": None,
        "measured_at": measured_at,
    }
    if (
        not clean_name
        or clean_name.startswith("-")
        or any(ch in clean_name for ch in ("/", "\\", "\x00", "\r", "\n"))
    ):
        base["error"] = "Ungültiger Remote-Name"
        return base

    try:
        completed = run(
            ["rclone", "about", "--json", "--", f"{clean_name}:"],
            env=rclone_subprocess_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        base["error"] = "Zeitüberschreitung bei rclone about"
        return base
    except OSError as exc:
        base["error"] = str(exc)
        return base

    if getattr(completed, "returncode", 1) != 0:
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        stdout = str(getattr(completed, "stdout", "") or "").strip()
        detail = (stderr or stdout)[:300]
        base["error"] = (
            detail or f"rclone about fehlgeschlagen (exit {completed.returncode})"
        )
        return base

    try:
        payload = json.loads(str(getattr(completed, "stdout", "") or "{}"))
    except (TypeError, ValueError):
        base["error"] = "Ungültige JSON-Antwort von rclone about"
        return base

    if not isinstance(payload, Mapping):
        base["error"] = "Ungültige Antwortstruktur von rclone about"
        return base

    total = _opt_int(payload.get("total"))
    used = _opt_int(payload.get("used"))
    free = _opt_int(payload.get("free"))
    trashed = _opt_int(payload.get("trashed"))
    other = _opt_int(payload.get("other"))
    percent_used = (
        round(used * 100.0 / total, 1)
        if total is not None and total > 0 and used is not None
        else None
    )
    base.update(
        {
            "ok": True,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "trashed_bytes": trashed,
            "other_bytes": other,
            "percent_used": percent_used,
            "error": None,
        }
    )
    return base


def _run_matches_remote(
    result: Mapping[str, Any], pair_name: str, history_key: str, remote_name: str
) -> bool:
    for field in ("remote", "local", "target"):
        if _extract_remote_name(result.get(field)) == remote_name:
            return True
    if _extract_remote_name(pair_name) == remote_name:
        return True
    prefix = f"{remote_name.casefold()}:"
    if prefix in str(history_key or "").casefold():
        return True
    return False


def estimate_growth_and_days_left(
    db: Any,
    remote_name: str,
    free_bytes: int | None,
    *,
    now: float | None = None,
    window_days: int = 30,
) -> dict[str, Any]:
    clean_name = str(remote_name or "").strip().rstrip(":")
    days = max(1, int(window_days))
    now_ts = time.time() if now is None else float(now)
    cutoff = now_ts - days * 86400
    total_uploaded = 0

    if db is not None and clean_name:
        with db.conn() as connection:
            rows = connection.execute(
                "SELECT pair_name, history_key, result_json FROM pair_runs "
                "WHERE ok=1 AND dry_run=0 AND started_at >= ?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(result, Mapping):
                continue
            if not _run_matches_remote(
                result,
                str(row["pair_name"] or ""),
                str(row["history_key"] or ""),
                clean_name,
            ):
                continue
            direction = str(result.get("direction") or "push").lower().strip()
            if direction not in {"push", "bisync"}:
                continue
            total_uploaded += _extract_transferred_bytes(result)

    daily_upload = total_uploaded / float(days)
    days_until_full: float | None = None
    if free_bytes is not None and free_bytes > 0 and daily_upload > 0:
        raw_days = free_bytes / daily_upload
        if raw_days <= 36500:
            days_until_full = round(raw_days, 1)

    return {
        "daily_upload_bytes": int(round(daily_upload)),
        "days_until_full": days_until_full,
    }


def monthly_transfer_usage(
    db: Any,
    config: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    cfg = _as_mapping(config)
    settings = budget_settings(cfg)
    tz = _configured_timezone(cfg)
    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts, tz)
    month_start_dt = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_ts = month_start_dt.timestamp()

    backup = cfg.get("backup") if isinstance(cfg.get("backup"), Mapping) else {}
    pairs_by_name: dict[str, Mapping[str, Any]] = {}
    for item in (backup or {}).get("pairs") or []:
        if isinstance(item, Mapping) and str(item.get("name") or "").strip():
            pairs_by_name[str(item.get("name")).strip()] = item

    upload_bytes = 0
    egress_bytes = 0
    by_remote: dict[str, dict[str, int]] = {}

    if db is not None:
        with db.conn() as connection:
            pair_rows = connection.execute(
                "SELECT pair_name, result_json FROM pair_runs "
                "WHERE dry_run=0 AND started_at >= ?",
                (month_start_ts,),
            ).fetchall()
            job_rows = connection.execute(
                "SELECT kind, summary_json FROM jobs "
                "WHERE dry_run=0 AND started_at >= ? "
                "AND kind IN ('restoretest', 'bitrot_scrub', 'bitrotscrub', 'recovery')",
                (month_start_ts,),
            ).fetchall()

        for row in pair_rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(result, Mapping):
                continue
            transferred = _extract_transferred_bytes(result)
            if transferred <= 0:
                continue
            pair_name = str(row["pair_name"] or result.get("name") or "").strip()
            pair_cfg = pairs_by_name.get(pair_name, {})
            direction = (
                str(result.get("direction") or pair_cfg.get("direction") or "push")
                .lower()
                .strip()
            )
            remote_name = (
                _extract_remote_name(result.get("remote"))
                or _extract_remote_name(result.get("local"))
                or _extract_remote_name(pair_cfg.get("remote"))
                or _extract_remote_name(pair_cfg.get("local"))
            )
            if direction == "pull":
                egress_bytes += transferred
            else:
                upload_bytes += transferred
            if remote_name:
                bucket = by_remote.setdefault(
                    remote_name, {"upload_bytes": 0, "egress_bytes": 0}
                )
                if direction == "pull":
                    bucket["egress_bytes"] += transferred
                else:
                    bucket["upload_bytes"] += transferred

        for row in job_rows:
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(summary, Mapping):
                continue
            raw_bytes = (
                summary.get("total_bytes")
                or summary.get("checked_bytes")
                or summary.get("bytes")
                or 0
            )
            val = _opt_int(raw_bytes)
            if val:
                egress_bytes += val

    egress_limit_bytes = settings["monthly_egress_gb"] * GIB
    upload_limit_bytes = settings["monthly_upload_gb"] * GIB
    egress_percent = (
        round(egress_bytes * 100.0 / egress_limit_bytes, 1)
        if egress_limit_bytes > 0
        else None
    )
    upload_percent = (
        round(upload_bytes * 100.0 / upload_limit_bytes, 1)
        if upload_limit_bytes > 0
        else None
    )
    warn_pct = settings["warn_percent"]
    enabled = settings["enabled"]

    egress_exceeded = bool(
        enabled and egress_limit_bytes > 0 and egress_bytes >= egress_limit_bytes
    )
    upload_exceeded = bool(
        enabled and upload_limit_bytes > 0 and upload_bytes >= upload_limit_bytes
    )
    egress_warning = bool(
        enabled
        and egress_limit_bytes > 0
        and egress_percent is not None
        and egress_percent >= warn_pct
    )
    upload_warning = bool(
        enabled
        and upload_limit_bytes > 0
        and upload_percent is not None
        and upload_percent >= warn_pct
    )
    throttle_optional_active = bool(
        enabled and settings["throttle_optional_jobs"] and egress_exceeded
    )

    return {
        "enabled": enabled,
        "month_start": month_start_dt.isoformat(),
        "month_start_ts": month_start_ts,
        "timezone": tz.key,
        "warn_percent": warn_pct,
        "throttle_optional_jobs": settings["throttle_optional_jobs"],
        "egress_bytes": egress_bytes,
        "upload_bytes": upload_bytes,
        "egress_limit_bytes": egress_limit_bytes,
        "upload_limit_bytes": upload_limit_bytes,
        "monthly_egress_gb": settings["monthly_egress_gb"],
        "monthly_upload_gb": settings["monthly_upload_gb"],
        "egress_percent": egress_percent,
        "upload_percent": upload_percent,
        "egress_warning": egress_warning,
        "upload_warning": upload_warning,
        "egress_exceeded": egress_exceeded,
        "upload_exceeded": upload_exceeded,
        "throttle_optional_active": throttle_optional_active,
        "by_remote": by_remote,
    }


def should_throttle_optional_job(
    db: Any,
    config: Mapping[str, Any],
    *,
    now: float | None = None,
) -> tuple[bool, str]:
    usage = monthly_transfer_usage(db, config, now=now)
    if usage.get("throttle_optional_active"):
        return (
            True,
            "Monatliches Egress-Budget überschritten – optionale Prüf- und Drill-Läufe sind gedrosselt",
        )
    return False, ""


def clear_quota_cache() -> None:
    with _cache_lock:
        _about_cache.clear()


def quota_report(
    config: Mapping[str, Any],
    db: Any,
    *,
    refresh: bool = False,
    run: Callable[..., Any] = subprocess.run,
    now: float | None = None,
) -> dict[str, Any]:
    cfg = _as_mapping(config)
    now_ts = time.time() if now is None else float(now)
    remotes_list: list[dict[str, Any]] = []

    for remote_name in configured_remotes(cfg):
        about: dict[str, Any] | None = None
        if not refresh:
            with _cache_lock:
                cached = _about_cache.get(remote_name)
                if cached is not None and (now_ts - cached[0]) <= CACHE_TTL_SECONDS:
                    about = dict(cached[1])
        if about is None:
            about = remote_about(remote_name, run=run)
            with _cache_lock:
                _about_cache[remote_name] = (now_ts, dict(about))
        growth = estimate_growth_and_days_left(
            db,
            remote_name,
            about.get("free_bytes"),
            now=now_ts,
        )
        remotes_list.append({**about, **growth})

    return {
        "generated_at": now_ts,
        "remotes": remotes_list,
        "budget": monthly_transfer_usage(db, cfg, now=now_ts),
    }


__all__ = [
    "CACHE_TTL_SECONDS",
    "DEFAULT_BUDGET",
    "budget_settings",
    "clear_quota_cache",
    "configured_remotes",
    "estimate_growth_and_days_left",
    "monthly_transfer_usage",
    "quota_report",
    "remote_about",
    "should_throttle_optional_job",
]
