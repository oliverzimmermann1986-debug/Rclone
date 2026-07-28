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
import random
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from ..config_store import get_config
from ..notifications import notify
from ..rclone_args import rclone_subprocess_env
from ..utils import bounded_int as _bounded_int
from .rclone_sync import (
    DEFAULT_CANCEL_SCOPE,
    _filter_args,
    _rclone_cache_args,
    _run_rclone_command,
    _safe_name,
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


def restore_test_settings(cfg) -> dict[str, Any]:
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
    rng: random.Random,
) -> dict[str, Any]:
    """Reservoir-Sampling über ein gestreamtes rclone-Listing.

    Reservoir statt "erste N": sonst träfe die Stichprobe immer dieselben
    alphabetisch führenden Dateien und ein defekter Bereich am Ende bliebe
    für immer unentdeckt.
    """
    cmd = [
        "rclone",
        "lsf",
        "--recursive",
        "--files-only",
        *_rclone_cache_args(),
        "--",
        target,
    ]
    reservoir: list[str] = []
    scanned = 0
    truncated = False
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
    deadline = time.monotonic() + _LISTING_TIMEOUT_SEC
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            path = line.rstrip("\n").rstrip("\r")
            if not path or path.endswith("/"):
                continue
            scanned += 1
            if len(reservoir) < sample_size:
                reservoir.append(path)
            else:
                index = rng.randrange(scanned)
                if index < sample_size:
                    reservoir[index] = path
            if scanned >= max_scan:
                truncated = True
                break
            if time.monotonic() >= deadline:
                truncated = True
                break
            if is_cancelled():
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stderr = ""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read() or ""
            except (OSError, ValueError):
                stderr = ""
            proc.stderr.close()
        if proc.stdout is not None:
            proc.stdout.close()

    if scanned == 0 and proc.returncode not in (0, None) and not truncated:
        raise RuntimeError(
            f"Listing von {target} fehlgeschlagen (exit {proc.returncode}): "
            f"{stderr.strip()[:300]}"
        )
    return {"paths": reservoir, "scanned": scanned, "truncated": truncated}


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
    result: dict[str, Any] = {
        "name": name,
        "source": source,
        "target": copy_target,
        "sample_size": 0,
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
    try:
        sample = _sample_paths(
            copy_target,
            sample_size=int(settings["sample_files"]),
            max_scan=int(settings["max_scan_files"]),
            rng=rng,
        )
        result["scanned"] = sample["scanned"]
        result["truncated"] = sample["truncated"]
        paths = sample["paths"]
        if not paths:
            # Ein leeres Ziel ist kein bestandener Drill, sondern ein Befund.
            result.update(
                {
                    "ok": False,
                    "error": "Ziel enthält keine Dateien — nichts zu prüfen",
                }
            )
            return result

        result["sample_size"] = len(paths)
        listing = _write_file_list(paths, workdir)
        filter_args = _filter_args(cfg, dict(pair), "check")

        copy_cmd = [
            "rclone",
            "copy",
            *_rclone_cache_args(),
            "--files-from-raw",
            str(listing),
            "--max-size",
            f"{int(settings['max_total_mb'])}M",
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
                    "return_code": rc,
                    "error": f"Rückholen fehlgeschlagen (rclone copy exit {rc})",
                }
            )
            return result

        pulled = sum(1 for item in restored.rglob("*") if item.is_file())
        result["restored_files"] = pulled
        if pulled == 0:
            result.update(
                {
                    "ok": False,
                    "error": "Keine Datei zurückgeholt — Stichprobe evtl. größer "
                    f"als max_total_mb ({settings['max_total_mb']} MB)",
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
            append=True,
            header=f"\n# Prüfsummenvergleich gegen {source}\n",
            pair_name=f"restoretest:{name}",
        )
        if is_cancelled():
            result.update({"ok": False, "cancelled": True, "error": "Abgebrochen"})
            return result
        result["return_code"] = rc
        if rc == 0:
            result.update({"ok": True, "verified": pulled})
        else:
            # Bewusst ohne Log-Auszug: rclone check listet die abweichenden
            # Dateipfade auf. Die gehören ins Logfile mit 0600, nicht ins
            # Job-Summary — das landet über recent_jobs im Support-Bundle
            # (Datenminimierung, Art. 5 Abs. 1 lit. c DSGVO).
            result.update(
                {
                    "ok": False,
                    "error": (
                        "Prüfsummen weichen ab oder Dateien fehlen in der Quelle "
                        f"(rclone check exit {rc}) — Details im Joblog"
                    ),
                }
            )
    except subprocess.TimeoutExpired:
        result.update(
            {"ok": False, "error": f"Timeout nach {round(timeout_sec / 3600, 1)} h"}
        )
    except Exception as exc:
        logger.exception("Restore-Drill für %s fehlgeschlagen", name)
        result.update({"ok": False, "error": str(exc)})
    finally:
        # Produktivdaten dürfen nicht liegen bleiben — auch nicht nach Abbruch
        # oder Ausnahme.
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            logger.error(
                "Temp-Verzeichnis %s konnte nicht entfernt werden; "
                "enthält zurückgeholte Produktivdaten",
                workdir,
            )
            result["temp_cleanup_failed"] = True
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
) -> dict[str, Any]:
    """Drill über alle ausgewählten Pairs. Rückgabe im Job-Summary-Format."""
    cfg = get_config()
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
