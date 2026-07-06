"""rclone Sync-/Backup-Jobs für Web-UI, CLI und Scheduler."""
from __future__ import annotations

import logging
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config_store import get_config

logger = logging.getLogger(__name__)

# rclone braucht ein beschreibbares Cache-Verzeichnis. Bei bisync ist zusätzlich
# --workdir wichtig, sonst landen Lock-/Listing-Dateien in ~/.cache/rclone/bisync.
RCLONE_CACHE_DIR = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")

_ACTIVE_PROCS: List[subprocess.Popen] = []
_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_PAIR_LOGS: Dict[str, str] = {}
_ACTIVE_PAIR_LOGS_LOCK = threading.Lock()
_CANCEL_EVENT = threading.Event()


def _rclone_cache_args(verb: Optional[str] = None) -> List[str]:
    Path(RCLONE_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    args = ["--cache-dir", RCLONE_CACHE_DIR]
    if verb == "bisync":
        workdir = f"{RCLONE_CACHE_DIR}/bisync"
        Path(workdir).mkdir(parents=True, exist_ok=True)
        args += ["--workdir", workdir]
    return args


def get_active_pair_logs() -> Dict[str, str]:
    with _ACTIVE_PAIR_LOGS_LOCK:
        return dict(_ACTIVE_PAIR_LOGS)


def _set_active_pair_log(name: str, log_file: Optional[Path]) -> None:
    with _ACTIVE_PAIR_LOGS_LOCK:
        if log_file is None:
            _ACTIVE_PAIR_LOGS.pop(name, None)
        else:
            _ACTIVE_PAIR_LOGS[name] = str(log_file)


def _register_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.append(proc)


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_LOCK:
        try:
            _ACTIVE_PROCS.remove(proc)
        except ValueError:
            pass


def _terminate_proc(proc: subprocess.Popen, *, graceful_sec: int = 10) -> None:
    """Beendet rclone robust inklusive Prozessgruppe."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=graceful_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_job() -> dict:
    """Killt alle laufenden rclone-Subprozesse und stoppt neue Pair-Starts."""
    _CANCEL_EVENT.set()
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_PROCS)
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            _terminate_proc(proc)
            killed += 1
    try:
        from ..notifications import notify
        notify("cancelled", "Sync abgebrochen", f"{killed} rclone-Prozess(e) beendet")
    except Exception:
        pass
    return {"ok": True, "killed": killed}


def is_cancelled() -> bool:
    return _CANCEL_EVENT.is_set()


def reset_cancel() -> None:
    _CANCEL_EVENT.clear()


def _is_remote(path: str) -> bool:
    if not path or path.startswith("/"):
        return False
    return ":" in path


def _safe_name(value: str, fallback: str = "pair") -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("._")
    return value[:80] or fallback


def _parse_rclone_args(value) -> List[str]:
    """rclone_args kann String oder Liste sein. Quotes werden korrekt beachtet."""
    if not value:
        return []
    out: List[str] = []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            out.extend(shlex.split(item))
        except ValueError:
            logger.warning("Ungültige rclone_args ignoriert: %r", item)
    return out

def _split_lines(value) -> List[str]:
    """Normalisiert mehrzeilige UI-Felder oder Listen zu non-empty Strings."""
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).splitlines()
    return [str(x).strip() for x in raw if str(x).strip() and not str(x).strip().startswith("#")]


def _structured_rclone_args(settings: Dict[str, Any], *, verb: Optional[str] = None) -> List[str]:
    """Erzeugt sichere rclone-Flags aus strukturierten Config-Feldern.

    Vorteil gegenüber Freitext-rclone_args: UI/Doctor kann Werte erklären und
    validieren. Unbekannte Felder werden ignoriert, raw overrides bleiben über
    rclone_args weiterhin möglich.
    """
    if not isinstance(settings, dict):
        return []
    args: List[str] = []
    numeric_or_string = {
        "transfers": "--transfers",
        "checkers": "--checkers",
        "retries": "--retries",
        "low_level_retries": "--low-level-retries",
        "tpslimit": "--tpslimit",
        "tpslimit_burst": "--tpslimit-burst",
        "max_transfer": "--max-transfer",
        "max_duration": "--max-duration",
        "contimeout": "--contimeout",
        "timeout": "--timeout",
        "bwlimit": "--bwlimit",
        "log_level": "--log-level",
    }
    for key, flag in numeric_or_string.items():
        value = settings.get(key)
        if value is None or value == "":
            continue
        args.append(f"{flag}={value}")

    # Löschschutz nur bei Kommandos, die wirklich löschen können.
    if verb in ("sync", "bisync"):
        max_delete = settings.get("max_delete")
        if max_delete not in (None, ""):
            args.append(f"--max-delete={max_delete}")
        if settings.get("delete_excluded"):
            args.append("--delete-excluded")

    bool_flags = {
        "fast_list": "--fast-list",
        "track_renames": "--track-renames",
        "metadata": "--metadata",
        "create_empty_src_dirs": "--create-empty-src-dirs",
        "ignore_existing": "--ignore-existing",
        "ignore_errors": "--ignore-errors",
        "drive_acknowledge_abuse": "--drive-acknowledge-abuse",
    }
    for key, flag in bool_flags.items():
        if settings.get(key):
            args.append(flag)
    return args


def _pair_filter_args(pair: Dict[str, Any]) -> List[str]:
    """Pair-spezifische Include/Exclude/Filter-Regeln aus der UI."""
    args: List[str] = []
    for pat in _split_lines(pair.get("include")):
        # UI verzeiht sowohl "*.jpg" als auch Filter-Syntax "+ *.jpg".
        if pat.startswith("+ "):
            pat = pat[2:].strip()
        args += ["--include", pat]
    for pat in _split_lines(pair.get("exclude")):
        # UI verzeiht sowohl ".DS_Store" als auch Filter-Syntax "- .DS_Store".
        if pat.startswith("- "):
            pat = pat[2:].strip()
        args += ["--exclude", pat]
    for rule in _split_lines(pair.get("filter")):
        args += ["--filter", rule]
    for key, flag in (("include_file", "--include-from"), ("exclude_file", "--exclude-from"), ("filter_file", "--filter-from")):
        value = str(pair.get(key) or "").strip()
        if value:
            args += [flag, value]
    return args


def command_to_string(cmd: List[str]) -> str:
    """Shell-lesbare Darstellung ohne Ausführung."""
    return " ".join(shlex.quote(str(c)) for c in cmd)


def _pair_warnings(pair: Dict[str, Any], *, dry_run: bool) -> List[str]:
    warnings: List[str] = []
    name = pair.get("name") or "?"
    direction = (pair.get("direction") or "bisync").lower().strip()
    mode = (pair.get("mode") or "bisync").lower().strip()
    if direction in ("pull", "push") and mode == "sync" and not dry_run:
        warnings.append(f"{name}: mode=sync kann Dateien im Ziel löschen. Erst Dry-Run prüfen.")
    if direction == "bisync" and pair.get("min_local_files", 1) in (0, "0"):
        warnings.append(f"{name}: min_local_files=0 deaktiviert den Mount-Schutz bei bisync.")
    if pair.get("exclude") and pair.get("include"):
        warnings.append(f"{name}: include und exclude kombiniert — Reihenfolge der Filter ist wichtig.")
    if not pair.get("remote") or not pair.get("local"):
        warnings.append(f"{name}: remote/local unvollständig.")
    return warnings


def _count_files_up_to(path: Path, limit: int) -> int:
    """Zählt nur bis limit. Spart bei großen Medienordnern massive Laufzeit."""
    if limit <= 0:
        return 0
    count = 0
    for root, _dirs, files in os.walk(path):
        count += len(files)
        if count >= limit:
            return count
    return count


def _run_rclone_command(cmd: List[str], log_file: Path, *, timeout_sec: int, append: bool = False,
                        header: Optional[str] = None) -> int:
    mode = "a" if append else "w"
    with open(log_file, mode, encoding="utf-8") as f:
        if header:
            f.write(header)
            f.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _register_proc(proc)
        try:
            return int(proc.wait(timeout=timeout_sec))
        except subprocess.TimeoutExpired:
            _terminate_proc(proc)
            raise
        finally:
            _unregister_proc(proc)


def _backup_extra_args(cfg) -> List[str]:
    extra: List[str] = []
    backup_cfg = cfg.get("backup", default={}) or {}

    filter_file = backup_cfg.get("filter_file") or ""
    if filter_file and Path(filter_file).is_file():
        extra += ["--filter-from", filter_file]
    elif filter_file:
        logger.warning("filter_file gesetzt aber nicht vorhanden: %s", filter_file)

    bwlimit = (backup_cfg.get("bwlimit") or "").strip()
    if bwlimit:
        extra += ["--bwlimit", bwlimit]

    conflict = (backup_cfg.get("conflict_resolve") or "auto").strip()
    if conflict and conflict != "auto":
        extra += ["--conflict-resolve", conflict]

    if backup_cfg.get("immutable"):
        extra += ["--immutable"]
    return extra


def _pair_safety_args(cfg, pair_root: str) -> List[str]:
    extra: List[str] = []
    backup_cfg = cfg.get("backup", default={}) or {}
    backup_dir = (backup_cfg.get("backup_dir") or "").strip()
    if backup_dir and pair_root:
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
        resolved = backup_dir.replace("{date}", stamp)
        if not _is_remote(resolved) and not resolved.startswith("/"):
            sep = "" if pair_root.endswith(("/", ":")) else "/"
            resolved = f"{pair_root}{sep}{resolved}"
        extra += ["--backup-dir", resolved]
    return extra


def _remote_reachable(path: str, timeout: int = 15) -> Tuple[bool, str]:
    if not path:
        return False, "Pfad leer"
    if _is_remote(path):
        try:
            r = subprocess.run(
                ["rclone", "lsf", path, "--max-depth", "1", *_rclone_cache_args()],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if r.returncode == 0:
                return True, "ok"
            err = (r.stderr or r.stdout or "")[:300].strip()
            if "directory not found" in err.lower() or "not found" in err.lower():
                return True, "directory empty/new"
            return False, f"rclone lsf exit={r.returncode}: {err}"
        except subprocess.TimeoutExpired:
            return False, f"timeout nach {timeout}s"
        except FileNotFoundError:
            return False, "rclone Binary nicht gefunden"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    p = Path(path)
    if p.exists() and p.is_dir():
        return True, "ok"
    if p.parent.exists():
        return True, "wird beim ersten Sync angelegt"
    return False, f"weder Pfad noch Parent existiert: {path}"


def _rclone_stats_remote(remote: str) -> Tuple[int, str]:
    try:
        c = subprocess.run(
            ["rclone", "lsf", remote, "--recursive", "--files-only", *_rclone_cache_args()],
            capture_output=True,
            text=True,
            timeout=120,
        )
        files = len(c.stdout.splitlines()) if c.returncode == 0 else 0
        s = subprocess.run(
            ["rclone", "size", remote, *_rclone_cache_args()],
            capture_output=True,
            text=True,
            timeout=120,
        )
        size = "?"
        if s.returncode == 0:
            m = re.search(r"Total size:\s+([^\n]+)", s.stdout)
            if m:
                size = m.group(1).strip()
        return files, size
    except Exception as e:
        logger.error("rclone stats %s: %s", remote, e)
        return 0, "?"


def _local_stats(path: str) -> Tuple[int, str]:
    try:
        p = Path(path)
        if not p.exists():
            return 0, "0"
        files = 0
        for _root, _dirs, names in os.walk(p):
            files += len(names)
        d = subprocess.run(["du", "-sh", path], capture_output=True, text=True, timeout=60)
        size = d.stdout.split()[0] if d.returncode == 0 and d.stdout.split() else "?"
        return files, size
    except Exception as e:
        logger.error("local stats %s: %s", path, e)
        return 0, "?"


def _pair_stats(path: str) -> Tuple[int, str]:
    return _rclone_stats_remote(path) if _is_remote(path) else _local_stats(path)


def _build_pair_command(pair: Dict, base_args: List[str], dry_run: bool) -> Tuple[List[str], str, str, str]:
    remote = pair["remote"]
    local = pair["local"]
    cfg = get_config()
    backup_cfg = cfg.get("backup", default={}) or {}

    direction = (pair.get("direction") or "bisync").lower().strip()
    mode = (pair.get("mode") or "bisync").lower().strip()
    if direction == "bisync":
        verb, src, dst = "bisync", remote, local
    elif direction == "pull":
        verb, src, dst = ("sync" if mode == "sync" else "copy"), remote, local
    elif direction == "push":
        verb, src, dst = ("sync" if mode == "sync" else "copy"), local, remote
    else:
        logger.warning("[%s] unbekannte direction=%r, fallback bisync", pair.get("name"), direction)
        verb, src, dst, direction = "bisync", remote, local, "bisync"

    pair_args = _parse_rclone_args(pair.get("rclone_args"))
    pair_safety = _pair_safety_args(cfg, remote)
    structured_global = _structured_rclone_args(backup_cfg.get("tuning") or {}, verb=verb)
    structured_pair = _structured_rclone_args(pair.get("options") or pair, verb=verb)
    filter_args = _pair_filter_args(pair)
    effective_args = list(base_args) + structured_global + structured_pair + filter_args + pair_args + pair_safety

    stats_interval = str((backup_cfg.get("tuning") or {}).get("stats_interval") or "10s")
    cmd = ["rclone", verb, src, dst, *_rclone_cache_args(verb), "--stats", stats_interval, "--stats-one-line"]
    cmd += effective_args
    if dry_run and "--dry-run" not in cmd:
        cmd.append("--dry-run")
    return cmd, verb, direction, mode

def _sync_pair(pair: Dict, args: List[str], log_dir: Path, dry_run: bool, timeout_sec: int) -> Dict:
    name = pair["name"]
    remote = pair["remote"]
    local = pair["local"]

    summary = {
        "name": name,
        "remote": remote,
        "local": local,
        "log_file": "",
        "ok": False,
        "error": "",
        "transferred": 0,
    }

    rok, rmsg = _remote_reachable(remote)
    lok, lmsg = _remote_reachable(local)
    if not rok or not lok:
        msg = f"Pre-Check fail (remote: {rmsg} / local: {lmsg})"
        logger.error("[%s] %s", name, msg)
        try:
            from ..notifications import notify
            notify("mount_check_failed", f"⚠ Pair '{name}' Pre-Check fehlgeschlagen", msg, pair=name)
        except Exception:
            pass
        summary.update({"error": msg, "skipped": True})
        return summary

    if not _is_remote(local):
        min_files = pair.get("min_local_files", 1)
        if min_files is None:
            min_files = 1
        try:
            min_files_i = int(min_files)
        except (TypeError, ValueError):
            min_files_i = 1
        if min_files_i > 0:
            local_path = Path(local)
            if not local_path.exists():
                msg = f"Lokaler Pfad existiert nicht: {local}"
                logger.error("[%s] Mount-Check: %s", name, msg)
                try:
                    from ..notifications import notify
                    notify("mount_check_failed", f"⚠ Pair '{name}' — Mount fehlt",
                           f"{msg}\n\nSync ABGEBROCHEN um Cloud-Löschung zu verhindern.", pair=name)
                except Exception:
                    pass
                summary.update({"error": msg, "skipped": True})
                return summary
            file_count = _count_files_up_to(local_path, min_files_i)
            if file_count < min_files_i:
                msg = f"Nur {file_count} Files unter {local}, min_local_files={min_files_i}. Mount-Drop vermutet."
                logger.error("[%s] Mount-Check: %s", name, msg)
                try:
                    from ..notifications import notify
                    notify("mount_check_failed", f"⚠ Pair '{name}' — verdächtig wenige Files",
                           f"{msg}\n\nSync ABGEBROCHEN. Falls Absicht: min_local_files im Pair auf 0 setzen.", pair=name)
                except Exception:
                    pass
                summary.update({"error": msg, "skipped": True})
                return summary
            logger.info("[%s] Mount-Check ok (%s Files >= %s)", name, file_count, min_files_i)

    if not _is_remote(local):
        Path(local).mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"sync-{_safe_name(name)}-{datetime.now():%Y%m%d-%H%M%S}.log"
    summary["log_file"] = str(log_file)
    _set_active_pair_log(name, log_file)

    cloud_files, cloud_size = _pair_stats(remote)
    local_files_before, local_size_before = _pair_stats(local)
    summary.update({
        "cloud_files": cloud_files,
        "cloud_size": cloud_size,
        "local_files_before": local_files_before,
        "local_size_before": local_size_before,
    })

    cmd, verb, direction, mode = _build_pair_command(pair, args, dry_run)
    logger.info("[%s] [%s/%s] %s", name, direction, mode, " ".join(shlex.quote(c) for c in cmd))

    try:
        if is_cancelled():
            summary["error"] = "vor Start abgebrochen"
            _set_active_pair_log(name, None)
            return summary
        rc = _run_rclone_command(cmd, log_file, timeout_sec=timeout_sec)
        log_content = log_file.read_text(errors="ignore") if log_file.exists() else ""

        auto_resync = bool((get_config().get("backup", default={}) or {}).get("auto_resync", False))
        needs_resync = verb == "bisync" and rc != 0 and "Must run --resync" in log_content
        if needs_resync and auto_resync:
            logger.info("[%s] auto --resync", name)
            cmd_resync = cmd + ["--resync"]
            if not is_cancelled():
                rc = _run_rclone_command(
                    cmd_resync,
                    log_file,
                    timeout_sec=timeout_sec,
                    append=True,
                    header="\n\n=== AUTO --resync ===\n\n",
                )
                log_content = log_file.read_text(errors="ignore")
        elif needs_resync:
            summary["resync_required"] = True
            logger.warning("[%s] bisync verlangt --resync; auto_resync ist deaktiviert", name)

        summary["ok"] = (rc == 0)
        summary["return_code"] = rc
        if not summary["ok"]:
            summary["error"] = "rclone verlangt --resync" if summary.get("resync_required") else f"rclone exit {rc}"
        summary["transferred"] = sum(
            1 for ln in log_content.splitlines()
            if "Copied" in ln or ("Transferred:" in ln and "/" in ln)
        )
    except subprocess.TimeoutExpired:
        summary["error"] = f"Timeout nach {round(timeout_sec / 3600, 1)}h"
        logger.error("[%s] Timeout", name)
    except Exception as e:
        summary["error"] = str(e)
        logger.exception("[%s] Exception", name)

    lf, ls = _pair_stats(local)
    summary["local_files_after"] = lf
    summary["local_size_after"] = ls
    if cloud_files and lf and direction == "bisync" and cloud_files != lf:
        summary["warning"] = "Cloud/Lokal Anzahl unterschiedlich"
    _set_active_pair_log(name, None)
    return summary


def _selected_pairs(pairs: Iterable[Dict], pairs_filter: Optional[list]) -> List[Dict]:
    out = [p for p in pairs if p.get("enabled", True)]
    if pairs_filter:
        wanted = {str(p).strip() for p in pairs_filter if str(p).strip()}
        out = [p for p in out if p.get("name") in wanted]
    return out


def run_job(dry_run: bool = False, pairs_filter: Optional[list] = None) -> Dict:
    cfg = get_config()
    backup_cfg = cfg.get("backup", default={}) or {}
    if not backup_cfg.get("enabled", True):
        logger.info("Backup deaktiviert")
        return {"enabled": False, "ok": True}

    all_pairs = backup_cfg.get("pairs") or []
    pairs = _selected_pairs(all_pairs, pairs_filter)
    if not pairs:
        return {"enabled": True, "ok": False, "error": "Keine aktiven Sync-Paare passend zur Auswahl"}

    reset_cancel()

    if not dry_run:
        try:
            from ..notifications import notify
            notify("sync_started", "Sync gestartet", f"{len(pairs)} Pair(s): {', '.join(p.get('name', '?') for p in pairs)}")
        except Exception:
            pass

    args = _parse_rclone_args(backup_cfg.get("rclone_args"))
    args += _backup_extra_args(cfg)
    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    log_dir.mkdir(parents=True, exist_ok=True)

    timeout_hours = float(backup_cfg.get("timeout_hours", 4) or 4)
    timeout_sec = max(300, int(timeout_hours * 3600))
    max_parallel = int(backup_cfg.get("max_parallel", 2) or 2)
    max_parallel = max(1, min(max_parallel, len(pairs)))

    logger.info("Starte Sync mit %d Worker(n) für %d Pair(s)", max_parallel, len(pairs))
    start = time.time()
    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = {ex.submit(_sync_pair, p, args, log_dir, dry_run, timeout_sec): p for p in pairs}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                logger.exception("Pair-Worker fehlgeschlagen")
                results.append({"name": futures[fut].get("name", "?"), "ok": False, "error": str(e)})

    duration = time.time() - start
    total_transferred = sum(r.get("transferred", 0) for r in results)
    ok_count = sum(1 for r in results if r.get("ok"))
    summary = {
        "enabled": True,
        "ok": ok_count == len(pairs),
        "started_at": datetime.fromtimestamp(start).isoformat(),
        "duration_sec": round(duration, 1),
        "dry_run": dry_run,
        "pairs": results,
        "ok_count": ok_count,
        "total_pairs": len(pairs),
        "total_transferred": total_transferred,
        "cancelled": is_cancelled(),
    }

    if not dry_run:
        try:
            from ..notifications import notify
            if summary["ok"]:
                notify("sync_ok", f"✓ Sync erfolgreich ({ok_count}/{len(pairs)} Paare)",
                       f"Dauer: {duration:.0f}s · Transfers/Stats-Zeilen: {total_transferred}", summary=summary)
            else:
                fail_names = [r.get("name", "?") for r in results if not r.get("ok")]
                notify("sync_error", f"✗ Sync mit Fehlern ({ok_count}/{len(pairs)})",
                       f"Fehlgeschlagen: {', '.join(fail_names)}\nDauer: {duration:.0f}s", summary=summary)
        except Exception as e:
            logger.warning("notify failed (non-fatal): %s", e)

    return summary


def build_job_plan(dry_run: bool = True, pairs_filter: Optional[list] = None) -> Dict[str, Any]:
    """Erstellt eine ausführbare Vorschau ohne rclone zu starten."""
    cfg = get_config()
    backup_cfg = cfg.get("backup", default={}) or {}
    all_pairs = backup_cfg.get("pairs") or []
    pairs = _selected_pairs(all_pairs, pairs_filter)
    args = _parse_rclone_args(backup_cfg.get("rclone_args"))
    args += _backup_extra_args(cfg)
    planned: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for pair in pairs:
        try:
            cmd, verb, direction, mode = _build_pair_command(pair, args, dry_run=dry_run)
            pw = _pair_warnings(pair, dry_run=dry_run)
            warnings.extend(pw)
            planned.append({
                "name": pair.get("name"),
                "enabled": pair.get("enabled", True),
                "remote": pair.get("remote"),
                "local": pair.get("local"),
                "verb": verb,
                "direction": direction,
                "mode": mode,
                "dry_run": dry_run,
                "command": command_to_string(cmd),
                "warnings": pw,
            })
        except Exception as e:
            planned.append({"name": pair.get("name"), "ok": False, "error": str(e)})
            warnings.append(f"{pair.get('name', '?')}: Plan konnte nicht gebaut werden: {e}")
    return {
        "ok": not any(p.get("error") for p in planned),
        "dry_run": dry_run,
        "total_pairs": len(planned),
        "pairs": planned,
        "warnings": warnings,
    }


def run_pair_check(pair_name: str, *, one_way: Optional[bool] = None, download: bool = False) -> Dict[str, Any]:
    """Read-only Integritätscheck per `rclone check` für ein Pair."""
    cfg = get_config()
    backup_cfg = cfg.get("backup", default={}) or {}
    pairs = [p for p in (backup_cfg.get("pairs") or []) if p.get("name") == pair_name]
    if not pairs:
        return {"ok": False, "error": f"Pair nicht gefunden: {pair_name}"}
    pair = pairs[0]
    direction = (pair.get("direction") or "bisync").lower().strip()
    if direction == "push":
        src, dst = pair.get("local"), pair.get("remote")
    else:
        src, dst = pair.get("remote"), pair.get("local")
    if one_way is None:
        one_way = direction in ("pull", "push")

    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"check-{_safe_name(pair_name)}-{datetime.now():%Y%m%d-%H%M%S}.log"
    args: List[str] = []
    filter_file = backup_cfg.get("filter_file") or ""
    if filter_file and Path(filter_file).is_file():
        args += ["--filter-from", filter_file]
    bwlimit = (backup_cfg.get("bwlimit") or (backup_cfg.get("tuning") or {}).get("bwlimit") or "")
    if str(bwlimit).strip():
        args += ["--bwlimit", str(bwlimit).strip()]
    # Für check nur read-only/sichere Flags verwenden; backup_dir/conflict/immutable
    # gehören zu Sync-Läufen und können bei `rclone check` ungültig sein.
    args += _pair_filter_args(pair)
    args += _parse_rclone_args(backup_cfg.get("check_args"))
    cmd = ["rclone", "check", src, dst, *_rclone_cache_args(), "--stats", "10s", "--stats-one-line"] + args
    if one_way:
        cmd.append("--one-way")
    if download:
        cmd.append("--download")
    timeout_hours = float(backup_cfg.get("timeout_hours", 4) or 4)
    timeout_sec = max(300, int(timeout_hours * 3600))
    result = {
        "pair": pair_name,
        "src": src,
        "dst": dst,
        "one_way": one_way,
        "download": download,
        "log_file": str(log_file),
        "command": command_to_string(cmd),
    }
    try:
        rc = _run_rclone_command(cmd, log_file, timeout_sec=timeout_sec)
        result["return_code"] = rc
        result["ok"] = rc == 0
        if rc != 0:
            result["error"] = f"rclone check exit {rc}"
        return result
    except subprocess.TimeoutExpired:
        result.update({"ok": False, "error": f"Timeout nach {round(timeout_sec / 3600, 1)}h"})
        return result
    except Exception as e:
        logger.exception("pair check failed")
        result.update({"ok": False, "error": str(e)})
        return result

def run_quick(remote_path: str, local_path: str, direction: str = "bisync",
              mode: str = "bisync", dry_run: bool = False,
              extra_args: Optional[list] = None) -> Dict:
    cfg = get_config()
    rok, rmsg = _remote_reachable(remote_path)
    lok, lmsg = _remote_reachable(local_path)
    if not rok or not lok:
        return {"ok": False, "error": f"Pre-Check fail (remote: {rmsg} / local: {lmsg})",
                "skipped": True, "remote": remote_path, "local": local_path}

    log_dir = Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_name(f"{remote_path}-{local_path}", "quick")
    log_file = log_dir / f"quick-{safe_name}-{datetime.now():%Y%m%d-%H%M%S}.log"

    args = _parse_rclone_args(cfg.get("backup", "rclone_args", default=""))
    args += _backup_extra_args(cfg)
    if extra_args:
        args += _parse_rclone_args(extra_args)

    reset_cancel()
    summary = {
        "direction": direction,
        "mode": mode,
        "remote": remote_path,
        "local": local_path,
        "dry_run": dry_run,
        "log_file": str(log_file),
    }

    pair = {"name": "quick", "remote": remote_path, "local": local_path, "direction": direction, "mode": mode}
    cmd, verb, direction, mode = _build_pair_command(pair, args, dry_run)
    logger.info("[quick] %s", " ".join(shlex.quote(c) for c in cmd))
    summary["cmd"] = " ".join(shlex.quote(c) for c in cmd)
    summary["verb"] = verb

    timeout_hours = float((cfg.get("backup", default={}) or {}).get("timeout_hours", 4) or 4)
    timeout_sec = max(300, int(timeout_hours * 3600))
    try:
        if is_cancelled():
            summary.update({"ok": False, "error": "vor Start abgebrochen"})
            return summary
        rc = _run_rclone_command(cmd, log_file, timeout_sec=timeout_sec)
        summary["return_code"] = rc
        log_tail = log_file.read_text(errors="ignore")[-4096:] if log_file.exists() else ""

        auto_resync = bool((cfg.get("backup", default={}) or {}).get("auto_resync", False))
        if verb == "bisync" and rc != 0 and "Must run --resync" in log_tail:
            if auto_resync and not is_cancelled():
                logger.info("[quick] auto --resync")
                rc = _run_rclone_command(cmd + ["--resync"], log_file, timeout_sec=timeout_sec,
                                         append=True, header="\n\n=== AUTO --resync ===\n\n")
                summary["resync_return_code"] = rc
            else:
                summary["resync_required"] = True

        summary["ok"] = (rc == 0)
        if not summary["ok"]:
            summary["error"] = "rclone verlangt --resync" if summary.get("resync_required") else f"rclone exit {rc}"
        return summary
    except subprocess.TimeoutExpired:
        summary.update({"ok": False, "error": f"Timeout nach {round(timeout_sec / 3600, 1)}h"})
        return summary
    except Exception as e:
        logger.exception("quick sync failed")
        summary.update({"ok": False, "error": str(e)})
        return summary
