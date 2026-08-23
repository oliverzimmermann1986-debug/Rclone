"""Restore-Drill: Stichproben aus dem Backup zurückholen und vergleichen.

Ein Sync-Lauf beweist, dass Daten geschrieben wurden. Er beweist nicht, dass
sie wieder lesbar sind und inhaltlich stimmen. Dieser Drill holt eine
Zufallsstichprobe aus dem Ziel in ein Temp-Verzeichnis und vergleicht sie per
Prüfsumme mit der lebenden Quelle.

Datenschutz: Die zurückgeholten Dateien sind Produktivdaten. Sie liegen nur
für die Dauer des Vergleichs im Temp-Verzeichnis, werden anschließend
unbedingt gelöscht (auch bei Abbruch) und landen weder im Job-Summary noch im
Support-Bundle. Protokolliert werden ausschließlich Pfadnamen und Zähler.
"""

from __future__ import annotations

import logging
import queue
import random
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from ..config_store import get_config
from ..notifications import notify
from ..rclone_args import rclone_subprocess_env
from ..utils import bounded_int as _bounded_int
from .rclone_sync import (
    DEFAULT_CANCEL_SCOPE,
    _SnapshotConfig,
    _filter_args,
    _rclone_cache_args,
    _register_proc,
    _run_rclone_command,
    _safe_name,
    _terminate_proc,
    _unregister_proc,
    command_to_string,
    is_cancelled,
    reset_cancel,
)

logger = logging.getLogger(__name__)

PAIR_PREFIX = "restore:"
JOB_KIND = "restoretest"
# Muss zu scheduler.RESTORE_TEST_HISTORY_KEY passen; dort importiert, um einen
# Zirkelimport zu vermeiden.
HISTORY_KEY = "restoretest:global"
AGGREGATE_RUN_NAME = "restore-drill"

# Ein vollständiges rekursives Listing kann bei Millionen Objekten Stunden
# dauern und Egress kosten. Der Scan wird darum gedeckelt; die Stichprobe ist
# dann über den gelesenen Präfix gleichverteilt, nicht über den Gesamtbestand.
# Das Ergebnis weist das über "truncated" aus.
_DEFAULT_MAX_SCAN = 20_000
_LISTING_TIMEOUT_SEC = 900
_FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
_LISTING_CANDIDATE_FACTOR = 8


def restore_test_settings(cfg) -> dict[str, Any]:
    if isinstance(cfg, Mapping):
        backup = cfg.get("backup") or {}
    else:
        backup = cfg.get("backup", default={}) or {}
    raw = backup.get("restore_test")
    if not isinstance(raw, Mapping):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "schedule": str(raw.get("schedule") or "manual").strip(),
        "sample_files": _bounded_int(
            raw.get("sample_files", 20), default=20, minimum=1, maximum=500
        ),
        "max_total_mb": _bounded_int(
            raw.get("max_total_mb", 256), default=256, minimum=1, maximum=51_200
        ),
        "max_scan_files": _bounded_int(
            raw.get("max_scan_files", _DEFAULT_MAX_SCAN),
            default=_DEFAULT_MAX_SCAN,
            minimum=100,
            maximum=1_000_000,
        ),
    }


def _endpoints(pair: Mapping[str, Any]) -> tuple[str, str]:
    """Liefert (Quelle, Sicherungskopie) — gleiche Regel wie run_pair_check."""
    direction = str(pair.get("direction") or "bisync").lower().strip()
    if direction == "push":
        return str(pair.get("local") or ""), str(pair.get("remote") or "")
    return str(pair.get("remote") or ""), str(pair.get("local") or "")


def _sample_paths(
    target: str,
    *,
    sample_size: int,
    max_scan: int,
    max_total_bytes: int,
    rng: random.Random,
    timeout_sec: float = _LISTING_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Reservoir-Sampling über ein gestreamtes, größenbewusstes Listing.

    Reservoir statt "erste N": sonst träfe die Stichprobe immer dieselben
    alphabetisch führenden Dateien und ein defekter Bereich am Ende bliebe
    für immer unentdeckt. Unbekannt große Dateien werden nicht übertragen;
    nur eine Stichprobe, deren Summe das harte Byte-Budget einhält, verlässt
    diese Funktion.
    """
    if sample_size <= 0 or max_scan <= 0 or max_total_bytes <= 0:
        raise ValueError(
            "Stichprobengröße, Scanlimit und Byte-Budget müssen positiv sein"
        )
    cmd = [
        "rclone",
        "lsf",
        "--recursive",
        "--files-only",
        "--format",
        "sp",
        "--separator",
        "\t",
        *_rclone_cache_args(),
        "--",
        target,
    ]
    # Ein etwas größeres Reservoir erhält die zufällige Streuung, während die
    # anschließende Budgetauswahl große Kandidaten überspringen kann.
    candidate_limit = min(
        max_scan, max(sample_size, sample_size * _LISTING_CANDIDATE_FACTOR)
    )
    reservoir: list[tuple[str, int]] = []
    scanned = 0
    eligible = 0
    unknown_size = 0
    oversized = 0
    truncated = False
    timed_out = False
    cancelled = False
    stop_reason = ""
    stderr_tail: deque[str] = deque(maxlen=40)
    stdout_events: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=256)
    stop_readers = threading.Event()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        close_fds=True,
        env=rclone_subprocess_env(),
    )
    registered = False

    def _pump_stdout() -> None:
        stream = proc.stdout
        if stream is None:
            stdout_events.put(("eof", None))
            return
        try:
            for line in stream:
                if stop_readers.is_set():
                    continue
                while not stop_readers.is_set():
                    try:
                        stdout_events.put(("line", line), timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            while True:
                try:
                    stdout_events.put(("eof", None), timeout=0.1)
                    break
                except queue.Full:
                    if stop_readers.is_set():
                        break

    def _pump_stderr() -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for line in stream:
                stderr_tail.append(line)
        except (OSError, ValueError):
            return

    deadline = time.monotonic() + max(0.01, float(timeout_sec))
    stdout_thread = threading.Thread(
        target=_pump_stdout, name="restore-listing-stdout", daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump_stderr, name="restore-listing-stderr", daemon=True
    )
    try:
        try:
            _register_proc(proc, pair_name="restoretest:listing")
            registered = True
        except Exception:
            _terminate_proc(proc, graceful_sec=2)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            _unregister_proc(proc)
            raise
        stdout_thread.start()
        stderr_thread.start()
        stdout_done = False
        while True:
            if is_cancelled():
                cancelled = True
                stop_reason = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                truncated = True
                stop_reason = "timeout"
                break
            try:
                kind, payload = stdout_events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if proc.poll() is not None and stdout_done:
                    break
                continue
            if kind == "eof":
                stdout_done = True
                if proc.poll() is not None:
                    break
                continue
            raw = str(payload or "").rstrip("\n").rstrip("\r")
            if not raw:
                continue
            try:
                size_text, path = raw.split("\t", 1)
            except ValueError:
                scanned += 1
                unknown_size += 1
            else:
                if not path or path.endswith("/"):
                    continue
                scanned += 1
                try:
                    size = int(size_text)
                    if size < 0:
                        raise ValueError
                except (ValueError, TypeError):
                    unknown_size += 1
                else:
                    if size > max_total_bytes:
                        oversized += 1
                    else:
                        eligible += 1
                        candidate = (path, size)
                        if len(reservoir) < candidate_limit:
                            reservoir.append(candidate)
                        else:
                            index = rng.randrange(eligible)
                            if index < candidate_limit:
                                reservoir[index] = candidate
            if scanned >= max_scan:
                truncated = True
                stop_reason = "scan_limit"
                break
    finally:
        stop_readers.set()
        try:
            if proc.poll() is None:
                _terminate_proc(proc, graceful_sec=2)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_proc(proc, graceful_sec=0)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error(
                        "rclone-Listingprozess %s ließ sich nicht reapen", proc.pid
                    )
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            if stdout_thread.ident is not None:
                stdout_thread.join(timeout=1)
            if stderr_thread.ident is not None:
                stderr_thread.join(timeout=1)
        finally:
            if registered:
                _unregister_proc(proc)

    stderr = "".join(stderr_tail).strip()
    if not stop_reason and proc.returncode not in (0, None):
        raise RuntimeError(
            f"Listing von {target} fehlgeschlagen (exit {proc.returncode}): "
            f"{stderr[:300]}"
        )
    rng.shuffle(reservoir)
    selected: list[tuple[str, int]] = []
    selected_bytes = 0
    for path, size in reservoir:
        if len(selected) >= sample_size:
            break
        if selected_bytes + size > max_total_bytes:
            continue
        selected.append((path, size))
        selected_bytes += size
    return {
        "paths": [path for path, _size in selected],
        "sizes": {path: size for path, size in selected},
        "selected_bytes": selected_bytes,
        "budget_bytes": max_total_bytes,
        "scanned": scanned,
        "eligible": eligible,
        "unknown_size": unknown_size,
        "oversized": oversized,
        "truncated": truncated,
        "timed_out": timed_out,
        "cancelled": cancelled,
    }


def _write_file_list(paths: list[str], directory: Path) -> Path:
    listing = directory / "sample.txt"
    listing.write_text("\n".join(paths) + "\n", encoding="utf-8")
    try:
        listing.chmod(0o600)
    except OSError:
        pass
    return listing


def run_pair_restore_test(
    pair: Mapping[str, Any],
    *,
    log_file: Path,
    settings: Mapping[str, Any],
    seed: Optional[int] = None,
) -> dict[str, Any]:
    """Drill für ein einzelnes Pair. Wirft nicht, meldet über das Ergebnis."""
    cfg = get_config()
    backup = cfg.get("backup", default={}) or {}
    name = str(pair.get("name") or "?")
    source, copy_target = _endpoints(pair)
    requested_sample_size = int(settings["sample_files"])
    result: dict[str, Any] = {
        "name": name,
        "source": source,
        "target": copy_target,
        "requested_sample_size": requested_sample_size,
        "sample_size": 0,
        "sample_status": "not_started",
        "verified": 0,
        "scanned": 0,
        "truncated": False,
        "log_file": str(log_file),
    }
    if not source or not copy_target:
        result.update({"ok": False, "error": "Quelle oder Ziel ist nicht gesetzt"})
        return result

    rng = random.Random(seed)
    temp_root = Path(
        cfg.get("paths", "temp_dir", default="/opt/rclone-sync/temp")
    ).expanduser()
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.update({"ok": False, "error": f"Temp-Verzeichnis nicht nutzbar: {exc}"})
        return result

    workdir = Path(
        tempfile.mkdtemp(prefix=f"restore-{_safe_name(name)}-", dir=str(temp_root))
    )
    try:
        workdir.chmod(0o700)
    except OSError:
        pass
    restored = workdir / "data"
    restored.mkdir(parents=True, exist_ok=True)

    timeout_sec = max(300, int(float(backup.get("timeout_hours", 4) or 4) * 3600))
    max_total_bytes = int(settings["max_total_mb"]) * 1024 * 1024
    result["budget_bytes"] = max_total_bytes
    try:
        result["sample_status"] = "listing"
        sample = _sample_paths(
            copy_target,
            sample_size=requested_sample_size,
            max_scan=int(settings["max_scan_files"]),
            max_total_bytes=max_total_bytes,
            rng=rng,
        )
        result["scanned"] = sample["scanned"]
        result["truncated"] = sample["truncated"]
        result["eligible_files"] = int(sample.get("eligible") or 0)
        result["skipped_unknown_size"] = int(sample.get("unknown_size") or 0)
        result["skipped_oversized"] = int(sample.get("oversized") or 0)
        result["selected_bytes"] = int(sample.get("selected_bytes") or 0)
        if sample.get("cancelled"):
            result.update(
                {
                    "ok": False,
                    "cancelled": True,
                    "sample_status": "cancelled",
                    "error": "Abgebrochen",
                }
            )
            return result
        if sample.get("timed_out"):
            result.update(
                {
                    "ok": False,
                    "sample_status": "listing_timeout",
                    "error": (
                        "Listing-Timeout nach "
                        f"{round(_LISTING_TIMEOUT_SEC / 60)} Minuten"
                    ),
                }
            )
            return result
        paths = sample["paths"]
        result["sample_size"] = len(paths)
        result["sample_shortfall"] = max(0, requested_sample_size - len(paths))
        if not paths:
            # Ein leeres Ziel ist kein bestandener Drill, sondern ein Befund.
            detail = "Ziel enthält keine Dateien innerhalb des sicheren Gesamtlimits"
            if result["skipped_unknown_size"]:
                detail += "; Dateien mit unbekannter Größe wurden sicher ausgeschlossen"
            result.update(
                {
                    "ok": False,
                    "sample_status": "no_candidates",
                    "error": detail,
                }
            )
            return result

        if len(paths) > requested_sample_size:
            raise RuntimeError(
                "Stichprobenauswahl enthält mehr Dateien als angefordert"
            )

        partial_selection = len(paths) < requested_sample_size
        if partial_selection:
            if result["truncated"]:
                shortfall_reason = "listing_truncated"
            elif result["eligible_files"] < requested_sample_size:
                shortfall_reason = "insufficient_eligible_files"
            else:
                shortfall_reason = "byte_budget"
            result.update(
                {
                    "sample_status": "partial_selection",
                    "sample_shortfall_reason": shortfall_reason,
                }
            )
        else:
            result["sample_status"] = "selected"

        selected_bytes = int(sample.get("selected_bytes") or 0)
        if selected_bytes < 0 or selected_bytes > max_total_bytes:
            raise RuntimeError("Stichprobe überschreitet das konfigurierte Gesamtlimit")
        usage = shutil.disk_usage(workdir)
        safety_margin = max(_FREE_SPACE_MARGIN_BYTES, selected_bytes // 10)
        required_space = selected_bytes + safety_margin
        result["free_space_bytes"] = int(usage.free)
        result["required_space_bytes"] = required_space
        if usage.free < required_space:
            result.update(
                {
                    "ok": False,
                    "error": (
                        "Nicht genügend freier Speicher für den Restore-Drill "
                        f"({usage.free} Byte frei, {required_space} Byte benötigt)"
                    ),
                }
            )
            return result

        listing = _write_file_list(paths, workdir)
        filter_args = _filter_args(cfg, dict(pair), "check")

        copy_cmd = [
            "rclone",
            "copy",
            *_rclone_cache_args(),
            "--files-from-raw",
            str(listing),
            "--max-transfer",
            str(max_total_bytes),
            "--cutoff-mode",
            "hard",
            "--stats",
            "10s",
            "--stats-one-line",
            *filter_args,
            "--",
            copy_target,
            str(restored),
        ]
        result["command"] = command_to_string(copy_cmd)
        rc = _run_rclone_command(
            copy_cmd,
            log_file,
            timeout_sec=timeout_sec,
            max_runtime_sec=timeout_sec,
            pair_name=f"restoretest:{name}",
            header=f"# Restore-Drill {name}: {len(paths)} Stichproben\n",
        )
        if is_cancelled():
            result.update({"ok": False, "cancelled": True, "error": "Abgebrochen"})
            return result
        if rc != 0:
            result.update(
                {
                    "ok": False,
                    "sample_status": "restore_failed",
                    "return_code": rc,
                    "error": f"Rückholen fehlgeschlagen (rclone copy exit {rc})",
                }
            )
            return result

        restored_items = [item for item in restored.rglob("*") if item.is_file()]
        pulled = len(restored_items)
        restored_bytes = sum(item.stat().st_size for item in restored_items)
        result["restored_files"] = pulled
        result["restored_bytes"] = restored_bytes
        if restored_bytes > max_total_bytes:
            result.update(
                {
                    "ok": False,
                    "error": "Rückgeholte Daten überschreiten das Gesamtlimit",
                }
            )
            return result
        if pulled == 0:
            result.update(
                {
                    "ok": False,
                    "sample_status": "restore_incomplete",
                    "error": "Keine Datei aus der ausgewählten Stichprobe zurückgeholt",
                }
            )
            return result

        expected_paths = {str(path).replace("\\", "/").lstrip("/") for path in paths}
        restored_paths = {
            item.relative_to(restored).as_posix().lstrip("/") for item in restored_items
        }
        if pulled != len(paths) or restored_paths != expected_paths:
            result.update(
                {
                    "ok": False,
                    "sample_status": "restore_incomplete",
                    "error": (
                        "Restore-Stichprobe unvollständig: "
                        f"{pulled} von {len(paths)} ausgewählten Dateien "
                        "zurückgeholt"
                    ),
                }
            )
            return result

        # --one-way: Nur die zurückgeholten Dateien müssen in der Quelle
        # existieren und übereinstimmen. Die Quelle darf mehr enthalten.
        check_cmd = [
            "rclone",
            "check",
            *_rclone_cache_args(),
            "--checksum",
            "--one-way",
            "--stats",
            "10s",
            "--stats-one-line",
            "--",
            str(restored),
            source,
        ]
        rc = _run_rclone_command(
            check_cmd,
            log_file,
            timeout_sec=timeout_sec,
            max_runtime_sec=timeout_sec,
            append=True,
            header=f"\n# Prüfsummenvergleich gegen {source}\n",
            pair_name=f"restoretest:{name}",
        )
        if is_cancelled():
            result.update(
                {
                    "ok": False,
                    "cancelled": True,
                    "sample_status": "cancelled",
                    "error": "Abgebrochen",
                }
            )
            return result
        result["return_code"] = rc
        if rc == 0:
            if partial_selection:
                result.update(
                    {
                        "ok": False,
                        "verified": pulled,
                        "error": (
                            "Teil-Stichprobe: "
                            f"{pulled} von {requested_sample_size} angeforderten "
                            "Dateien ausgewählt und erfolgreich geprüft"
                        ),
                    }
                )
            else:
                result.update(
                    {"ok": True, "verified": pulled, "sample_status": "complete"}
                )
        else:
            # Bewusst ohne Log-Auszug: rclone check listet die abweichenden
            # Dateipfade auf. Die gehören ins Logfile mit 0600, nicht ins
            # Job-Summary — das landet über recent_jobs im Support-Bundle
            # (Datenminimierung, Art. 5 Abs. 1 lit. c DSGVO).
            result.update(
                {
                    "ok": False,
                    "sample_status": "verification_failed",
                    "error": (
                        "Prüfsummen weichen ab oder Dateien fehlen in der Quelle "
                        f"(rclone check exit {rc}) — Details im Joblog"
                    ),
                }
            )
    except subprocess.TimeoutExpired as exc:
        result.update(
            {
                "ok": False,
                "sample_status": "runtime_timeout",
                "error": (
                    f"Maximale Laufzeit von {round(timeout_sec / 3600, 1)} h überschritten"
                    if getattr(exc, "watchdog_reason", "stalled") == "max_runtime"
                    else f"Kein rclone-Fortschritt seit {round(timeout_sec / 3600, 1)} h"
                ),
            }
        )
    except Exception as exc:
        logger.exception("Restore-Drill für %s fehlgeschlagen", name)
        result.update({"ok": False, "sample_status": "error", "error": str(exc)})
    finally:
        # Produktivdaten dürfen nicht liegen bleiben — auch nicht nach Abbruch
        # oder Ausnahme.
        cleanup_error = ""
        try:
            shutil.rmtree(workdir)
        except Exception as exc:
            cleanup_error = str(exc).strip() or type(exc).__name__
        if cleanup_error or workdir.exists():
            if not cleanup_error:
                cleanup_error = "Temp-Verzeichnis besteht nach der Bereinigung weiter"
            cleanup_detail = (
                f"Temp-Bereinigung fehlgeschlagen für {workdir}: {cleanup_error}"
            )
            logger.error(
                "Temp-Verzeichnis %s konnte nicht entfernt werden; "
                "enthält möglicherweise zurückgeholte Produktivdaten: %s",
                workdir,
                cleanup_error,
            )
            previous_error = str(result.get("error") or "").strip()
            result.update(
                {
                    "ok": False,
                    "temp_cleanup_failed": True,
                    "temp_cleanup_path": str(workdir),
                    "cleanup_error": cleanup_error,
                    "error": (
                        f"{previous_error}; {cleanup_detail}"
                        if previous_error
                        else cleanup_detail
                    ),
                }
            )
    return result


def _selectable_pairs(cfg, pairs_filter: Optional[list[str]]) -> list[dict[str, Any]]:
    backup = cfg.get("backup", default={}) or {}
    wanted = {str(item) for item in (pairs_filter or [])}
    selected = []
    for pair in backup.get("pairs") or []:
        if not isinstance(pair, Mapping):
            continue
        name = str(pair.get("name") or "")
        if not name:
            continue
        if wanted and name not in wanted:
            continue
        if not wanted and not pair.get("enabled", True):
            continue
        selected.append(dict(pair))
    return selected


def run_restore_test(
    pairs_filter: Optional[list[str]] = None,
    *,
    trigger: str = "manual",
    seed: Optional[int] = None,
    reset_cancel_state: bool = True,
    config_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Drill über alle ausgewählten Pairs. Rückgabe im Job-Summary-Format."""
    cfg = (
        _SnapshotConfig(dict(config_snapshot))
        if config_snapshot is not None
        else get_config()
    )
    settings = restore_test_settings(cfg)
    if reset_cancel_state:
        reset_cancel(DEFAULT_CANCEL_SCOPE)

    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    pairs = _selectable_pairs(cfg, pairs_filter)
    results: list[dict[str, Any]] = []
    for pair in pairs:
        name = str(pair.get("name") or "?")
        if is_cancelled():
            results.append(
                {"name": name, "ok": False, "cancelled": True, "error": "Abgebrochen"}
            )
            continue
        log_file = (
            log_dir
            / f"restoretest-{_safe_name(name)}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
        )
        results.append(
            run_pair_restore_test(pair, log_file=log_file, settings=settings, seed=seed)
        )

    ok = bool(results) and all(item.get("ok") for item in results)
    cancelled = any(item.get("cancelled") for item in results)
    verified = sum(int(item.get("verified") or 0) for item in results)
    sampled = sum(int(item.get("sample_size") or 0) for item in results)

    history_keys = {
        str(item.get("name")): f"{PAIR_PREFIX}{item.get('name')}"
        for item in results
        if item.get("name")
    }
    # Aggregatzeile: Der Scheduler verfolgt den Drill über einen einzigen
    # Historienschlüssel, weil er als ein Lauf über alle Pairs ausgeführt wird.
    aggregate = {
        "name": AGGREGATE_RUN_NAME,
        "ok": ok,
        "verified": verified,
        "sample_size": sampled,
        "pairs_tested": len(results),
    }
    if cancelled:
        aggregate["cancelled"] = True
    history_keys[AGGREGATE_RUN_NAME] = HISTORY_KEY

    summary: dict[str, Any] = {
        "ok": ok,
        "cancelled": cancelled,
        "trigger": trigger,
        "pairs": [*results, aggregate],
        "verified_files": verified,
        "sampled_files": sampled,
        "history_keys": history_keys,
    }
    if not results:
        summary["error"] = "Kein passendes Pair ausgewählt"

    _notify_result(summary, results)
    return summary


def _notify_result(summary: Mapping[str, Any], results: list[dict[str, Any]]) -> None:
    if not results or summary.get("cancelled"):
        return
    failed = [item for item in results if not item.get("ok")]
    if failed:
        detail = "\n".join(
            f"{item.get('name')}: {item.get('error') or 'unbekannter Fehler'}"
            for item in failed
        )
        notify(
            "restore_test_error",
            f"Restore-Drill fehlgeschlagen für {len(failed)} Pair(s)",
            detail,
            pairs=[str(item.get("name")) for item in failed],
        )
        return
    detail = "\n".join(
        f"{item.get('name')}: {item.get('verified')} von {item.get('sample_size')} "
        "Stichproben identisch"
        + (" (Listing gekürzt)" if item.get("truncated") else "")
        for item in results
    )
    notify(
        "restore_test_ok",
        f"Restore-Drill bestanden: {summary.get('verified_files')} Dateien geprüft",
        detail,
        pairs=[str(item.get("name")) for item in results],
    )


__all__ = [
    "AGGREGATE_RUN_NAME",
    "HISTORY_KEY",
    "JOB_KIND",
    "PAIR_PREFIX",
    "restore_test_settings",
    "run_pair_restore_test",
    "run_restore_test",
]
