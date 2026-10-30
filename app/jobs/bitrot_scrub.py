"""Rotierender Cloud-Bitrot-Scrub.

Holt regelmäßig eine kleine Stichprobe der ältesten Cloud-Dateien, bildet
SHA-256 und vergleicht mit der lokal gespeicherten Baseline. Weicht der Hash
bei unveränderter Größe und Änderungszeit ab, hat der Provider die Datei still
verändert.

Datenschutz: Inhalte werden nur gestreamt gehasht (``rclone hashsum
--download``) und nie gespeichert. Pfade stehen im Joblog und im Befund, nicht
in Push-Nachrichten.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..config_store import get_config
from ..db import get_db
from ..notifications import notify
from ..rclone_args import rclone_subprocess_env
from ..utils import bounded_int as _bounded_int
from .rclone_sync import (
    DEFAULT_CANCEL_SCOPE,
    _SnapshotConfig,
    _filter_args,
    _is_remote,
    _rclone_cache_args,
    _register_proc,
    _run_rclone_command,
    _safe_name,
    _terminate_proc,
    _unregister_proc,
    is_cancelled,
    reset_cancel,
)
from .restore_sampling import select_budgeted_sample
from .restore_test import _selectable_pairs
from .scheduler import (
    DEFAULT_TIMEZONE,
    _evaluate_due,
    _is_disabled,
    _is_valid_schedule,
    _load_history,
    next_run_after,
    rclone_history_key,
)

# Fester Name statt __name__: JobScopeFilter("bitrotscrub") nimmt nur
# Records unter app.jobs.bitrotscrub ins Joblog.
logger = logging.getLogger("app.jobs.bitrotscrub")

JOB_KIND = "bitrotscrub"
BITROT_HISTORY_KEY = "bitrotscrub:global"
AGGREGATE_RUN_NAME = "bitrot-scrub"
_LISTING_TIMEOUT_SEC = 900


def _backup_section(cfg) -> Mapping[str, Any]:
    if isinstance(cfg, Mapping):
        return cfg.get("backup") or {}
    return cfg.get("backup", default={}) or {}


def bitrot_scrub_settings(cfg) -> dict[str, Any]:
    raw = _backup_section(cfg).get("bitrot_scrub")
    if not isinstance(raw, Mapping):
        raw = {}
    # Grenzen wie in config_validation (dort für Snapshots bereits geklemmt).
    return {
        "enabled": bool(raw.get("enabled", False)),
        "schedule": str(raw.get("schedule") or "0 4 * * *").strip(),
        "sample_percent": _bounded_int(
            raw.get("sample_percent", 2), default=2, minimum=1, maximum=100
        ),
        "max_files": _bounded_int(
            raw.get("max_files", 200), default=200, minimum=1, maximum=5000
        ),
        "max_total_mb": _bounded_int(
            raw.get("max_total_mb", 256), default=256, minimum=1, maximum=10_240
        ),
        "max_scan_files": _bounded_int(
            raw.get("max_scan_files", 20_000),
            default=20_000,
            minimum=100,
            maximum=1_000_000,
        ),
        "rotation_days": _bounded_int(
            raw.get("rotation_days", 30), default=30, minimum=1, maximum=365
        ),
    }


def _cloud_endpoint(pair: Mapping[str, Any]) -> str:
    for key in ("remote", "local"):
        value = str(pair.get(key) or "").strip()
        if _is_remote(value):
            return value
    return ""


def _parse_lsjson_line(line: str) -> Optional[dict[str, Any]]:
    text = line.strip().rstrip(",")
    if not text.startswith("{"):
        return None
    try:
        item = json.loads(text)
    except ValueError:
        return None
    if not isinstance(item, dict) or item.get("IsDir"):
        return None
    path, size = item.get("Path"), item.get("Size")
    # --files-from-raw kann keine Zeilenumbrüche im Pfad ausdrücken.
    if not isinstance(path, str) or not path or "\n" in path or "\r" in path:
        return None
    return {
        "path": path,
        "size": size if type(size) is int else -1,
        "modtime": str(item.get("ModTime") or ""),
    }


def _list_files(
    remote: str,
    *,
    max_scan: int,
    filter_args: Sequence[str],
    log_file: Path,
    rng: Optional[random.Random] = None,
) -> dict[str, Any]:
    """Streamt ``rclone lsjson`` komplett; ein Reservoir (Algorithmus R) hält
    ``max_scan`` gleichverteilte Einträge, damit große Ziele nicht immer dieselben
    ersten Dateien liefern."""
    rng = rng or random.Random()
    cmd = [
        "rclone",
        "lsjson",
        "-R",
        "--files-only",
        "--no-mimetype",
        *_rclone_cache_args(),
        *filter_args,
        "--",
        remote,
    ]
    files: list[dict[str, Any]] = []
    scanned = 0
    timed_out = threading.Event()
    with log_file.open("a", encoding="utf-8") as log:
        try:
            os.chmod(log_file, 0o600)
        except OSError:
            pass
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=log,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            close_fds=True,
            env=rclone_subprocess_env(),
        )

        def _on_timeout() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(_LISTING_TIMEOUT_SEC, _on_timeout)
        try:
            _register_proc(
                proc, pair_name="bitrotscrub:listing", log_file=str(log_file)
            )
            timer.start()
            for line in proc.stdout:
                entry = _parse_lsjson_line(line)
                if entry is None:
                    continue
                scanned += 1
                if len(files) < max_scan:
                    files.append(entry)
                elif (slot := rng.randrange(scanned)) < max_scan:
                    files[slot] = entry
        finally:
            timer.cancel()
            _terminate_proc(proc, graceful_sec=2)
            proc.stdout.close()
            _unregister_proc(proc)
    if timed_out.is_set():
        raise RuntimeError(f"Listing-Timeout nach {_LISTING_TIMEOUT_SEC // 60} Minuten")
    if not is_cancelled() and proc.returncode != 0:
        raise RuntimeError(
            f"Listing fehlgeschlagen (rclone lsjson exit {proc.returncode}) "
            "— Details im Joblog"
        )
    return {"files": files, "scanned": scanned, "truncated": scanned > max_scan}


def _modtime_key(value: str) -> float:
    # Python 3.10 (CI) kennt in fromisoformat weder "Z" noch != 6 Nachkommastellen.
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return math.inf  # unlesbare Zeitstempel zuletzt


def _select_sample(
    files: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]],
    settings: Mapping[str, Any],
    *,
    now: float,
) -> list[Mapping[str, Any]]:
    """Älteste fällige Dateien: ohne Baseline oder seit rotation_days ungeprüft."""
    cutoff = now - settings["rotation_days"] * 86400
    due = sorted(
        (
            item
            for item in files
            if ((baseline.get(item["path"]) or {}).get("last_verified_at") or 0)
            < cutoff
        ),
        key=lambda item: _modtime_key(item["modtime"]),
    )
    target = max(
        1,
        min(
            settings["max_files"],
            math.ceil(len(files) * settings["sample_percent"] / 100),
        ),
    )
    chosen = select_budgeted_sample(
        [(item["path"], item["size"]) for item in due],
        target,
        settings["max_total_mb"] * 1024 * 1024,
    )
    by_path = {item["path"]: item for item in due}
    return [by_path[path] for path, _size in chosen]


def _parse_hashsum(text: str) -> dict[str, str]:
    """Liest Zeilen ``<sha256>  <pfad>``; alles andere wird ignoriert."""
    hashes: dict[str, str] = {}
    for line in text.split("\n"):
        digest, separator, path = line.rstrip("\r").partition("  ")
        digest = digest.lower()
        if (
            separator
            and path
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
        ):
            hashes[path] = digest
    return hashes


def _hashsum(
    remote: str, paths: list[str], *, log_file: Path, timeout_sec: float, name: str
) -> tuple[dict[str, str], int]:
    """SHA-256 per Streaming-Download; nur Pfadliste und Hashes liegen kurz im Temp."""
    workdir = Path(tempfile.mkdtemp(prefix="bitrot-"))
    try:
        listing = workdir / "files.txt"
        listing.write_text("\n".join(paths) + "\n", encoding="utf-8", newline="\n")
        output = workdir / "sha256.txt"
        cmd = [
            "rclone",
            "hashsum",
            "sha256",
            "--download",
            *_rclone_cache_args(),
            "--files-from-raw",
            str(listing),
            "--output-file",
            str(output),
            "--",
            remote,
        ]
        rc = _run_rclone_command(
            cmd,
            log_file,
            timeout_sec=timeout_sec,
            max_runtime_sec=timeout_sec,
            append=True,
            header=f"\n# Bitrot-Scrub {name}: {len(paths)} Dateien\n",
            pair_name=f"bitrotscrub:{name}",
        )
        text = (
            output.read_text(encoding="utf-8", errors="replace")
            if output.exists()
            else ""
        )
        return _parse_hashsum(text), rc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _compare(
    sample: Sequence[Mapping[str, Any]],
    hashes: Mapping[str, str],
    baseline: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Teilt gehashte Stichproben in Befunde, bestätigte und neu erfasste Dateien."""
    mismatches: list[dict[str, Any]] = []
    verified: list[str] = []
    recorded: list[dict[str, Any]] = []
    for item in sample:
        actual = hashes.get(item["path"])
        if actual is None:
            continue
        known = baseline.get(item["path"])
        if not known or (known["size"], known["modtime"]) != (
            item["size"],
            item["modtime"],
        ):
            recorded.append({**item, "sha256": actual})
        elif known["sha256"] == actual:
            verified.append(item["path"])
        else:
            mismatches.append(
                {
                    "path": item["path"],
                    "expected": known["sha256"],
                    "actual": actual,
                    "size": item["size"],
                }
            )
    return mismatches, verified, recorded


def _scrub_pair(
    pair: Mapping[str, Any],
    *,
    settings: Mapping[str, Any],
    cfg,
    db,
    log_file: Path,
) -> dict[str, Any]:
    """Scrub für ein Pair. Wirft nicht, meldet über das Ergebnis."""
    name = str(pair.get("name") or "?")
    pair_key = rclone_history_key(pair)
    result: dict[str, Any] = {
        "name": name,
        "ok": False,
        "scanned": 0,
        "sampled": 0,
        "verified": 0,
        "recorded": 0,
        "mismatches": [],
        "truncated": False,
        "error": None,
        "history_key": f"{JOB_KIND}:{pair_key}",
        "log_file": str(log_file),
    }
    remote = _cloud_endpoint(pair)
    timeout_hours = _backup_section(cfg).get("timeout_hours", 4) or 4
    timeout_sec = max(300, int(float(timeout_hours) * 3600))
    try:
        listing = _list_files(
            remote,
            max_scan=settings["max_scan_files"],
            filter_args=_filter_args(cfg, dict(pair), "lsjson"),
            log_file=log_file,
        )
        files = listing["files"]
        result.update(scanned=listing["scanned"], truncated=listing["truncated"])
        if is_cancelled():
            result.update(cancelled=True, error="Abgebrochen")
            return result
        baseline = db.hash_baseline_get_many(pair_key, [f["path"] for f in files])
        sample = _select_sample(files, baseline, settings, now=time.time())
        result["sampled"] = len(sample)
        if not sample:
            result["ok"] = True
            return result
        hashes, rc = _hashsum(
            remote,
            [item["path"] for item in sample],
            log_file=log_file,
            timeout_sec=timeout_sec,
            name=name,
        )
        if is_cancelled():
            result.update(cancelled=True, error="Abgebrochen")
            return result
        mismatches, verified, recorded = _compare(sample, hashes, baseline)
        db.hash_baseline_upsert(pair_key, recorded)
        # Befunde bleiben fällig, damit der Alarm bis zur Klärung wiederkehrt.
        db.hash_baseline_touch(
            pair_key, [*verified, *(item["path"] for item in recorded)], time.time()
        )
        db.hash_baseline_prune(pair_key)
        for item in mismatches:
            logger.warning(
                "[%s] Bitrot-Verdacht: %s (Baseline %s, gelesen %s)",
                name,
                item["path"],
                item["expected"],
                item["actual"],
            )
        result.update(
            verified=len(verified), recorded=len(recorded), mismatches=mismatches
        )
        hashed = len(verified) + len(recorded) + len(mismatches)
        if rc != 0:
            result["error"] = f"rclone hashsum exit {rc} — Details im Joblog"
        elif hashed < len(sample):
            result["error"] = f"Nur {hashed} von {len(sample)} Stichproben gehasht"
        result["ok"] = not result["error"] and not mismatches
    except subprocess.TimeoutExpired:
        # Ohne Exception-Text: er enthält das Kommando samt Remote-String.
        result["error"] = "Zeitlimit für rclone überschritten — Details im Joblog"
    except Exception as exc:
        logger.exception("Bitrot-Scrub für %s fehlgeschlagen", name)
        result["error"] = str(exc) or type(exc).__name__
    return result


def run_bitrot_scrub(
    pairs_filter: Optional[list[str]] = None,
    *,
    trigger: str = "web",
    config_snapshot: Optional[Mapping[str, Any]] = None,
    reset_cancel_state: bool = True,
) -> dict[str, Any]:
    """Scrub über alle ausgewählten Pairs mit Cloud-Remote; Job-Summary-Format."""
    started_at = time.time()
    snapshot = (
        copy.deepcopy(dict(config_snapshot))
        if config_snapshot is not None
        else get_config().snapshot()
    )
    cfg = _SnapshotConfig(snapshot)
    settings = bitrot_scrub_settings(cfg)
    if reset_cancel_state:
        reset_cancel(DEFAULT_CANCEL_SCOPE)
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    db = get_db()

    results: list[dict[str, Any]] = []
    for pair in _selectable_pairs(cfg, pairs_filter):
        name = str(pair.get("name") or "?")
        if not _cloud_endpoint(pair):
            logger.info("[%s] kein Cloud-Remote, übersprungen", name)
            continue
        if is_cancelled():
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "cancelled": True,
                    "error": "Abgebrochen",
                    "history_key": f"{JOB_KIND}:{rclone_history_key(pair)}",
                }
            )
            continue
        log_file = (
            log_dir
            / f"{JOB_KIND}-{_safe_name(name)}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
        )
        results.append(
            _scrub_pair(pair, settings=settings, cfg=cfg, db=db, log_file=log_file)
        )

    ok = bool(results) and all(item.get("ok") for item in results)
    cancelled = any(item.get("cancelled") for item in results)
    verified = sum(int(item.get("verified") or 0) for item in results)
    sampled = sum(int(item.get("sampled") or 0) for item in results)
    mismatch_count = sum(len(item.get("mismatches") or []) for item in results)
    # Aggregatzeile: Der Scheduler verfolgt den Scrub über einen globalen Schlüssel.
    aggregate: dict[str, Any] = {
        "name": AGGREGATE_RUN_NAME,
        "ok": ok,
        "verified": verified,
        "sampled": sampled,
        "mismatch_count": mismatch_count,
        "pairs_tested": len(results),
        "history_key": BITROT_HISTORY_KEY,
    }
    if cancelled:
        aggregate["cancelled"] = True
    summary: dict[str, Any] = {
        "kind": JOB_KIND,
        "ok": ok,
        "cancelled": cancelled,
        "trigger": trigger,
        "started_at": started_at,
        "pairs": [*results, aggregate],
        "verified_files": verified,
        "sampled_files": sampled,
        "mismatch_files": mismatch_count,
        "history_keys": {
            str(item["name"]): str(item["history_key"])
            for item in [*results, aggregate]
        },
    }
    if not results:
        summary["error"] = "Kein Pair mit Cloud-Remote ausgewählt"
    truncated_pairs = [str(item["name"]) for item in results if item.get("truncated")]
    if truncated_pairs:
        summary["warning"] = (
            "Stichprobe aus begrenztem Listing (max_scan_files): "
            + ", ".join(truncated_pairs)
        )
    _notify_result(results)
    return summary


def _notify_result(results: list[dict[str, Any]]) -> None:
    for item in results:
        mismatches = item.get("mismatches") or []
        if not mismatches:
            continue
        # Keine Pfade in die Push-Nachricht (Datenminimierung); Details im Joblog.
        notify(
            "bitrot_detected",
            f"{item.get('name')}: Bitrot-Verdacht",
            f"{len(mismatches)} Datei(en) mit abweichender SHA-256 bei "
            "unveränderter Größe und Änderungszeit — Details im Joblog",
            pairs=[str(item.get("name"))],
        )


def bitrot_scrub_due(cfg, db, *, now: Optional[float] = None) -> dict[str, Any]:
    """Fälligkeit des Scrubs; ein globaler Lauf wie beim Restore-Drill."""
    settings = bitrot_scrub_settings(cfg)
    if not settings["enabled"]:
        return {"due": False, "reason": "disabled"}
    schedule = settings["schedule"]
    if _is_disabled(schedule):
        return {"due": False, "reason": f"schedule={schedule}"}
    if not _is_valid_schedule(schedule):
        return {"due": False, "reason": "invalid_schedule", "error": schedule}
    backup = _backup_section(cfg)
    timezone_name = str(backup.get("timezone") or DEFAULT_TIMEZONE)
    now_value = float(time.time() if now is None else now)
    try:
        history = _load_history(db, {BITROT_HISTORY_KEY: BITROT_HISTORY_KEY})
        evaluation = _evaluate_due(
            schedule,
            history.get(BITROT_HISTORY_KEY) or {},
            history_key=BITROT_HISTORY_KEY,
            now=now_value,
            timezone_name=timezone_name,
            retry_sec=_bounded_int(
                backup.get("scheduler_retry_minutes", 60),
                default=60,
                minimum=1,
                maximum=10080,
            )
            * 60,
            grace_minutes=_bounded_int(
                backup.get("scheduler_grace_minutes", 15),
                default=15,
                minimum=1,
                maximum=1440,
            ),
            run_on_first_tick=False,
        )
    except Exception as exc:
        return {"due": False, "error": str(exc)}
    return {
        "history_key": BITROT_HISTORY_KEY,
        "schedule": schedule,
        "timezone": timezone_name,
        "next_run": next_run_after(
            schedule, after=now_value, timezone_name=timezone_name
        ),
        **evaluation,
    }


__all__ = [
    "AGGREGATE_RUN_NAME",
    "BITROT_HISTORY_KEY",
    "JOB_KIND",
    "bitrot_scrub_due",
    "bitrot_scrub_settings",
    "run_bitrot_scrub",
]
