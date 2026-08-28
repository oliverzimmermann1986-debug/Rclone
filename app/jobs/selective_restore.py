"""Selective recovery into an isolated staging directory.

The module never writes to a configured source or target. A successful recovery
remains in a private staging directory until the authenticated user removes it.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..db import Database
from ..notifications import notify
from ..rclone_args import rclone_subprocess_env
from .rclone_sync import _rclone_cache_args
from .restore_test import _endpoints

logger = logging.getLogger(__name__)
JOB_KIND = "recovery"
_STATE_KEY = "selective_recovery:v1"
_MAX_ITEMS = 100
_MAX_STATE_ITEMS = 100
_FREE_SPACE_MARGIN = 64 * 1024 * 1024


def normalize_selection(paths: Sequence[str]) -> list[str]:
    if not paths or len(paths) > _MAX_ITEMS:
        raise ValueError(f"Es müssen 1 bis {_MAX_ITEMS} Einträge ausgewählt werden")
    clean: list[str] = []
    for raw in paths:
        original = str(raw or "").replace("\\", "/").strip()
        if original.startswith("/"):
            raise ValueError("Auswahl enthält einen absoluten Pfad")
        value = original.strip("/")
        candidate = PurePosixPath(value)
        if (
            not value
            or len(value) > 1024
            or candidate.is_absolute()
            or ".." in candidate.parts
            or any(part in {"", "."} for part in candidate.parts)
            or any(char in value for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("Auswahl enthält einen unsicheren relativen Pfad")
        clean.append(candidate.as_posix())
    return list(dict.fromkeys(clean))


def staging_root(config: Mapping[str, Any]) -> Path:
    paths = config.get("paths") if isinstance(config.get("paths"), Mapping) else {}
    raw = str((paths or {}).get("recovery_dir") or "/opt/rclone-sync/recovery")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise ValueError("paths.recovery_dir muss absolut sein")
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root.resolve()


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )


def _load_state(database: Database) -> list[dict[str, Any]]:
    value = database.runtime_get(_STATE_KEY, [])
    return (
        [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, list)
        else []
    )


def _save_record(database: Database, record: Mapping[str, Any]) -> None:
    records = [
        item for item in _load_state(database) if item.get("id") != record.get("id")
    ]
    records.insert(0, dict(record))
    database.runtime_set(_STATE_KEY, records[:_MAX_STATE_ITEMS])


def list_staging(database: Database, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    root = staging_root(config)
    result = []
    for record in _load_state(database):
        candidate = (root / str(record.get("id") or "")).resolve()
        if candidate.parent != root:
            continue
        result.append({**record, "exists": candidate.is_dir()})
    return result


def remove_staging(
    database: Database, config: Mapping[str, Any], recovery_id: str
) -> bool:
    root = staging_root(config)
    if not recovery_id or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
        for char in recovery_id.lower()
    ):
        raise ValueError("Ungültige Recovery-ID")
    target = (root / recovery_id).resolve()
    if target.parent != root:
        raise ValueError("Recovery-Ziel liegt außerhalb des Staging-Bereichs")
    existed = target.is_dir()
    if existed:
        shutil.rmtree(target)
    records = [item for item in _load_state(database) if item.get("id") != recovery_id]
    database.runtime_set(_STATE_KEY, records)
    database.audit_add(
        "recovery_staging_removed", actor="web", details={"id": recovery_id}
    )
    return existed


def run_selective_restore(
    database: Database,
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    paths: Sequence[str],
    *,
    max_total_mb: int,
    job_id: int,
) -> dict[str, Any]:
    started = time.monotonic()
    selection = normalize_selection(paths)
    limit_bytes = max(1, min(int(max_total_mb), 51_200)) * 1024 * 1024
    _live_source, backup_target = _endpoints(pair)
    recovery_id = f"recovery-{job_id}"
    work = staging_root(config) / recovery_id
    data = work / "data"
    record: dict[str, Any] = {
        "id": recovery_id,
        "job_id": job_id,
        "pair": str(pair.get("name") or ""),
        "status": "running",
        "created_at": time.time(),
        "requested_items": len(selection),
        "limit_bytes": limit_bytes,
    }
    try:
        work.mkdir(mode=0o700)
        data.mkdir(mode=0o700)
        listing = work / "selection.txt"
        listing.write_text("\n".join(selection) + "\n", encoding="utf-8")
        try:
            listing.chmod(0o600)
        except OSError:
            pass
        timeout = max(
            300, int(float((config.get("backup") or {}).get("timeout_hours", 4)) * 3600)
        )
        size_cmd = [
            "rclone",
            "size",
            "--json",
            *_rclone_cache_args(),
            "--files-from-raw",
            str(listing),
            "--",
            backup_target,
        ]
        measured = _run(size_cmd, timeout=min(timeout, 900))
        if measured.returncode != 0:
            raise RuntimeError("Auswahl konnte am Sicherungsziel nicht gemessen werden")
        size = json.loads(measured.stdout or "{}")
        selected_bytes = int(size.get("bytes") or 0)
        selected_count = int(size.get("count") or 0)
        if selected_count <= 0:
            raise RuntimeError("Die Auswahl enthält am Sicherungsziel keine Dateien")
        if selected_bytes > limit_bytes:
            raise RuntimeError(
                "Die Auswahl überschreitet das bestätigte Recovery-Limit"
            )
        free = shutil.disk_usage(work).free
        if free < selected_bytes + _FREE_SPACE_MARGIN:
            raise RuntimeError("Nicht genügend freier Speicher im Recovery-Staging")
        copy_cmd = [
            "rclone",
            "copy",
            *_rclone_cache_args(),
            "--files-from-raw",
            str(listing),
            "--max-transfer",
            str(limit_bytes),
            "--cutoff-mode",
            "hard",
            "--stats",
            "10s",
            "--stats-one-line",
            "--",
            backup_target,
            str(data),
        ]
        copied = _run(copy_cmd, timeout=timeout)
        if copied.returncode != 0:
            raise RuntimeError(
                f"Recovery fehlgeschlagen (rclone exit {copied.returncode})"
            )
        check_cmd = [
            "rclone",
            "check",
            *_rclone_cache_args(),
            "--checksum",
            "--one-way",
            "--",
            str(data),
            backup_target,
        ]
        checked = _run(check_cmd, timeout=timeout)
        if checked.returncode != 0:
            raise RuntimeError(
                "Recovery-Prüfsummen stimmen nicht mit dem Sicherungsziel überein"
            )
        listing.unlink(missing_ok=True)
        record.update(
            {
                "status": "ready",
                "files": selected_count,
                "bytes": selected_bytes,
                "duration_sec": round(time.monotonic() - started, 2),
                "staging_path": str(data),
                "verified": True,
            }
        )
        database.job_finish(job_id, "ok", {"ok": True, **record})
        database.audit_add(
            "selective_recovery_ready",
            actor="system",
            details={
                key: record[key] for key in ("id", "job_id", "pair", "files", "bytes")
            },
        )
        notify(
            "recovery_ready",
            f"{record['pair']}: Recovery bereit",
            f"{selected_count} Dateien wurden getrennt wiederhergestellt und geprüft.",
            job_id=job_id,
            pair=record["pair"],
        )
    except Exception as exc:
        logger.exception("Selektive Recovery %s fehlgeschlagen", recovery_id)
        shutil.rmtree(work, ignore_errors=True)
        record.update(
            {
                "status": "error",
                "error": str(exc),
                "duration_sec": round(time.monotonic() - started, 2),
            }
        )
        database.job_finish(job_id, "error", {"ok": False, **record})
        notify(
            "recovery_error",
            f"{record['pair']}: Recovery fehlgeschlagen",
            str(exc),
            job_id=job_id,
            pair=record["pair"],
        )
    _save_record(database, record)
    return record


__all__ = [
    "JOB_KIND",
    "list_staging",
    "normalize_selection",
    "remove_staging",
    "run_selective_restore",
    "staging_root",
]
