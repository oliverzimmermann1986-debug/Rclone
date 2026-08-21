"""Robuste rclone-Sync-/Backup-Jobs für Web-UI, CLI und Scheduler."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..config_store import get_config
from ..rclone_args import (
    redact_command_text,
    rclone_subprocess_env,
    validate_parsed_rclone_args,
    validate_rclone_args,
)
from . import runtime_state
from .log_tail import read_tail
from .pair_planner import has_overlapping_pairs, pairs_conflict, paths_overlap
from .scheduler import rclone_history_key
from ..utils import bounded_number as _bounded_number

logger = logging.getLogger(__name__)

RCLONE_CACHE_DIR = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")
DEFAULT_CANCEL_SCOPE = runtime_state.DEFAULT_CANCEL_SCOPE

_ACTIVE_PROCS: list[tuple[subprocess.Popen, str]] = []
_ACTIVE_PROCS_LOCK = threading.Lock()
_ACTIVE_PAIR_LOGS: dict[str, str] = {}
_ACTIVE_PAIR_LOGS_LOCK = threading.Lock()


class _SnapshotConfig:
    """Read-only Config-compatible view over one job's configuration snapshot."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def get(self, *keys: str, default: Any = None) -> Any:
        current: Any = self.data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current


_CANCEL_EVENT = threading.Event()
_CANCEL_EVENTS: dict[str, threading.Event] = {
    DEFAULT_CANCEL_SCOPE: _CANCEL_EVENT,
}
_CANCEL_EVENTS_LOCK = threading.Lock()

_RESYNC_RE = re.compile(
    r"(?:must run[^\n]*--resync|requires?[^\n]*--resync)", re.IGNORECASE
)
_STATS_RE = re.compile(
    r"Transferred:\s*([^,\n]+?)(?:\s*/\s*([^,\n]+?))?"
    r"(?:,\s*(?:([\d.]+)%|-))?(?:,\s*([^,\n]+?/s))?(?:,\s*ETA\s*(\S+))?\s*$"
)


def _rclone_cache_args(verb: Optional[str] = None) -> list[str]:
    Path(RCLONE_CACHE_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(RCLONE_CACHE_DIR, 0o700)
    except OSError:
        pass
    args = ["--cache-dir", RCLONE_CACHE_DIR]
    if verb == "bisync":
        workdir = str(Path(RCLONE_CACHE_DIR) / "bisync")
        Path(workdir).mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(workdir, 0o700)
        except OSError:
            pass
        args += ["--workdir", workdir]
    return args


def read_log_tail(path: Path, max_bytes: int = 1024 * 1024) -> str:
    return read_tail(path, max_bytes=max_bytes)


def get_active_pair_logs() -> dict[str, str]:
    with _ACTIVE_PAIR_LOGS_LOCK:
        logs = dict(_ACTIVE_PAIR_LOGS)
    state = runtime_state.load_run_state() or {}
    if state.get("status") == "running":
        for name, pair in (state.get("pairs") or {}).items():
            if pair.get("status") == "running" and pair.get("log_file"):
                logs[str(name)] = str(pair["log_file"])
    return logs


def get_runtime_state() -> Optional[dict[str, Any]]:
    return runtime_state.load_run_state()


def _set_active_pair_log(name: str, log_file: Optional[Path]) -> None:
    with _ACTIVE_PAIR_LOGS_LOCK:
        if log_file is None:
            _ACTIVE_PAIR_LOGS.pop(name, None)
        else:
            _ACTIVE_PAIR_LOGS[name] = str(log_file)


def _register_proc(
    proc: subprocess.Popen,
    *,
    pair_name: str = "",
    log_file: str = "",
    cancel_scope: str = DEFAULT_CANCEL_SCOPE,
    run_id: str = "",
) -> None:
    normalized_scope = runtime_state._scope_name(cancel_scope)
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.append((proc, normalized_scope))
    executable = (
        str(proc.args[0])
        if isinstance(proc.args, (list, tuple)) and proc.args
        else "rclone"
    )
    runtime_state.register_process(
        proc.pid,
        pair_name=pair_name,
        log_file=log_file,
        scope=normalized_scope,
        executable=executable,
        run_id=run_id,
    )


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS[:] = [item for item in _ACTIVE_PROCS if item[0] is not proc]
    runtime_state.unregister_process(proc.pid)


def _terminate_proc(proc: subprocess.Popen, *, graceful_sec: int = 10) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=graceful_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _cancel_event(scope: str = DEFAULT_CANCEL_SCOPE) -> threading.Event:
    normalized_scope = runtime_state._scope_name(scope)
    with _CANCEL_EVENTS_LOCK:
        return _CANCEL_EVENTS.setdefault(normalized_scope, threading.Event())


def cancel_job(scope: str = DEFAULT_CANCEL_SCOPE) -> dict[str, Any]:
    """Beendet laufende rclone-Prozesse auch aus CLI-/Scheduler-Prozessen."""
    normalized_scope = runtime_state._scope_name(scope)
    _cancel_event(normalized_scope).set()
    runtime_state.request_cancel_marker(normalized_scope)
    killed = runtime_state.terminate_active_processes(
        graceful_sec=8, scope=normalized_scope
    )
    # Fallback für einen Marker-Schreibfehler im aktuellen Prozess.
    with _ACTIVE_PROCS_LOCK:
        local_procs = [
            proc for proc, proc_scope in _ACTIVE_PROCS if proc_scope == normalized_scope
        ]
    for proc in local_procs:
        if proc.poll() is None:
            _terminate_proc(proc, graceful_sec=2)

    def _notify_cancelled() -> None:
        try:
            from ..notifications import notify

            notify(
                "cancelled", "Sync abgebrochen", f"{killed} rclone-Prozess(e) beendet"
            )
        except Exception:
            logger.exception("Cancel-Benachrichtigung fehlgeschlagen")

    # Webhooks (bis zu 60s Timeout) dürfen den Cancel-Request nicht blockieren.
    threading.Thread(
        target=_notify_cancelled, name="notify-cancelled", daemon=True
    ).start()
    return {"ok": True, "killed": killed, "scope": normalized_scope}


def is_cancelled(scope: str = DEFAULT_CANCEL_SCOPE) -> bool:
    normalized_scope = runtime_state._scope_name(scope)
    return _cancel_event(normalized_scope).is_set() or runtime_state.cancel_requested(
        normalized_scope
    )


def reset_cancel(scope: str = DEFAULT_CANCEL_SCOPE) -> None:
    normalized_scope = runtime_state._scope_name(scope)
    _cancel_event(normalized_scope).clear()
    runtime_state.reset_cancel_marker(normalized_scope)


def _is_remote(path: str) -> bool:
    return bool(
        path
        and not Path(path).is_absolute()
        and not path.startswith("-")
        and ":" in path
    )


def _safe_name(value: str, fallback: str = "pair") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("._")
    return cleaned[:80] or fallback


def _parse_rclone_args(value: Any, *, allow_unsafe: bool = False) -> list[str]:
    return validate_rclone_args(value, allow_unsafe=allow_unsafe)


def _split_lines(value: Any) -> list[str]:
    if not value:
        return []
    raw = value if isinstance(value, list) else str(value).splitlines()
    return [
        str(item).strip()
        for item in raw
        if str(item).strip() and not str(item).strip().startswith("#")
    ]


def _structured_rclone_args(
    settings: dict[str, Any], *, verb: Optional[str] = None
) -> list[str]:
    if not isinstance(settings, dict):
        return []
    args: list[str] = []
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
        if value not in (None, ""):
            args.append(f"{flag}={value}")

    if verb in {"sync", "bisync"}:
        max_delete = settings.get("max_delete")
        if max_delete not in (None, ""):
            args.append(f"--max-delete={max_delete}")
        max_delete_size = settings.get("max_delete_size")
        if max_delete_size not in (None, ""):
            args.append(f"--max-delete-size={max_delete_size}")
        if settings.get("delete_excluded"):
            args.append("--delete-excluded")

    bool_flags = {
        "fast_list": "--fast-list",
        "track_renames": "--track-renames",
        "metadata": "--metadata",
        "create_empty_src_dirs": "--create-empty-src-dirs",
        "ignore_existing": "--ignore-existing",
        "drive_acknowledge_abuse": "--drive-acknowledge-abuse",
    }
    for key, flag in bool_flags.items():
        if settings.get(key):
            args.append(flag)

    # ignore_errors ist bei sync/bisync gefährlich, weil es Löschungen trotz
    # I/O-Fehlern erlauben kann. Nur mit bewusstem allow_unsafe_flags übernehmen.
    if settings.get("ignore_errors") and settings.get("allow_unsafe_flags"):
        args.append("--ignore-errors")
    return args


def _filter_args(cfg, pair: dict[str, Any], verb: str) -> list[str]:
    args: list[str] = []
    for pattern in _split_lines(pair.get("include")):
        args += [
            "--include",
            pattern[2:].strip() if pattern.startswith("+ ") else pattern,
        ]
    for pattern in _split_lines(pair.get("exclude")):
        args += [
            "--exclude",
            pattern[2:].strip() if pattern.startswith("- ") else pattern,
        ]
    for rule in _split_lines(pair.get("filter")):
        args += ["--filter", rule]

    for key, flag in (
        ("include_file", "--include-from"),
        ("exclude_file", "--exclude-from"),
    ):
        value = str(pair.get(key) or "").strip()
        if value:
            if not Path(value).is_file():
                raise ValueError(f"{key} nicht gefunden: {value}")
            args += [flag, value]

    # Pair-Datei überschreibt die globale Filterdatei. Bisync nutzt bewusst
    # --filters-file, damit rclone Änderungen hasht und einen Resync erzwingt.
    filter_file = str(
        pair.get("filter_file") or cfg.get("backup", "filter_file", default="") or ""
    ).strip()
    if filter_file:
        if not Path(filter_file).is_file():
            raise ValueError(
                f"filter_file gesetzt, aber nicht vorhanden: {filter_file}"
            )
        args += ["--filters-file" if verb == "bisync" else "--filter-from", filter_file]
    return args


def command_to_string(cmd: list[str]) -> str:
    rendered = " ".join(shlex.quote(str(item)) for item in cmd)
    return redact_command_text(rendered)


def _pair_warnings(pair: dict[str, Any], *, dry_run: bool) -> list[str]:
    warnings: list[str] = []
    name = pair.get("name") or "?"
    direction = str(pair.get("direction") or "bisync").lower().strip()
    mode = str(pair.get("mode") or "bisync").lower().strip()
    destructive = direction == "bisync" or (
        direction in {"pull", "push"} and mode == "sync"
    )
    if destructive and not dry_run:
        warnings.append(f"{name}: {mode} kann Dateien löschen. Erst Dry-Run prüfen.")
        if not pair.get("allow_delete"):
            warnings.append(
                f"{name}: produktiver {mode} ist ohne allow_delete gesperrt."
            )
        if pair.get("max_delete") in (None, "", -1, "-1"):
            warnings.append(f"{name}: kein begrenztes max_delete am Pair gesetzt.")
    if direction in {"bisync", "push"} and pair.get("min_local_files", 1) in (0, "0"):
        warnings.append(
            f"{name}: min_local_files=0 deaktiviert den lokalen Mount-/Quellschutz."
        )
    if pair.get("exclude") and pair.get("include"):
        warnings.append(
            f"{name}: include und exclude kombiniert — Filterreihenfolge prüfen."
        )
    if pair.get("require_mountpoint") and not pair.get("mountpoint"):
        warnings.append(
            f"{name}: require_mountpoint prüft mangels mountpoint den lokalen Pair-Pfad selbst."
        )
    if not pair.get("remote") or not pair.get("local"):
        warnings.append(f"{name}: remote/local unvollständig.")
    return warnings


def _count_files_up_to(path: Path, limit: int) -> int:
    if limit <= 0:
        return 0
    count = 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
        if count >= limit:
            return count
    return count


def _run_rclone_command(
    cmd: list[str],
    log_file: Path,
    *,
    timeout_sec: int,
    append: bool = False,
    header: Optional[str] = None,
    pair_name: str = "",
    extra_env: Optional[dict[str, str]] = None,
    cancel_scope: str = DEFAULT_CANCEL_SCOPE,
    run_id: str = "",
    pre_spawn_check: Optional[Callable[[], tuple[bool, str]]] = None,
) -> int:
    normalized_scope = runtime_state._scope_name(cancel_scope)
    mode = "a" if append else "w"
    with log_file.open(mode, encoding="utf-8") as handle:
        try:
            os.chmod(log_file, 0o600)
        except OSError:
            pass
        if header:
            handle.write(header)
            handle.flush()
        if is_cancelled(normalized_scope):
            return 130
        if pre_spawn_check is not None:
            check_ok, check_message = pre_spawn_check()
            if not check_ok:
                raise RuntimeError(
                    f"Sicherheits-Recheck direkt vor Prozessstart fehlgeschlagen: "
                    f"{check_message}"
                )
        if is_cancelled(normalized_scope):
            return 130
        proc = subprocess.Popen(
            cmd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={**rclone_subprocess_env(), **(extra_env or {})},
        )
        try:
            _register_proc(
                proc,
                pair_name=pair_name,
                log_file=str(log_file),
                cancel_scope=normalized_scope,
                run_id=run_id,
            )
        except Exception:
            _terminate_proc(proc, graceful_sec=2)
            _unregister_proc(proc)
            raise
        deadline = time.monotonic() + timeout_sec
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    return int(rc)
                if is_cancelled(normalized_scope):
                    _terminate_proc(proc)
                    return 130
                if time.monotonic() >= deadline:
                    _terminate_proc(proc)
                    raise subprocess.TimeoutExpired(cmd, timeout_sec)
                time.sleep(0.25)
        finally:
            _unregister_proc(proc)


def _global_verb_args(cfg, verb: str) -> list[str]:
    backup = cfg.get("backup", default={}) or {}
    args: list[str] = []
    bwlimit = str(backup.get("bwlimit") or "").strip()
    if bwlimit:
        args += ["--bwlimit", bwlimit]
    if backup.get("immutable"):
        args.append("--immutable")
    if verb == "bisync":
        conflict = str(backup.get("conflict_resolve") or "auto").strip().lower()
        if conflict not in {"", "auto", "none"}:
            args += ["--conflict-resolve", conflict]
        if backup.get("resilient", True):
            args.append("--resilient")
        if backup.get("recover", True):
            args.append("--recover")
        max_lock = str(backup.get("max_lock") or "2m").strip()
        if max_lock:
            args += ["--max-lock", max_lock]
    return args


def _resolve_backup_path(root: str, spec: str, stamp: str) -> str:
    resolved = spec.replace("{date}", stamp)
    if _is_remote(resolved) or resolved.startswith("/"):
        return resolved
    separator = "" if root.endswith(("/", ":")) else "/"
    return f"{root}{separator}{resolved}"


def _backup_dir_args(
    cfg, pair: dict[str, Any], verb: str, src: str, dst: str
) -> list[str]:
    backup = cfg.get("backup", default={}) or {}
    generic = str(pair.get("backup_dir") or backup.get("backup_dir") or "").strip()
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if verb == "bisync":
        dir1 = str(pair.get("backup_dir1") or backup.get("backup_dir1") or "").strip()
        dir2 = str(pair.get("backup_dir2") or backup.get("backup_dir2") or "").strip()
        if generic and not (_is_remote(generic) or generic.startswith("/")):
            dir1 = dir1 or generic
            dir2 = dir2 or generic
        args: list[str] = []
        if dir1:
            args += ["--backup-dir1", _resolve_backup_path(src, dir1, stamp)]
        if dir2:
            args += ["--backup-dir2", _resolve_backup_path(dst, dir2, stamp)]
        if generic and not dir1 and not dir2:
            logger.warning(
                "Absolutes backup_dir kann bei bisync nicht beiden Seiten sicher zugeordnet werden; nutze backup_dir1/backup_dir2"
            )
        return args
    if generic:
        return ["--backup-dir", _resolve_backup_path(dst, generic, stamp)]
    return []


def _remote_reachable(
    path: str, timeout: int = 15, *, allow_missing: bool = False
) -> tuple[bool, str]:
    if not path:
        return False, "Pfad leer"
    if _is_remote(path):
        try:
            result = subprocess.run(
                [
                    "rclone",
                    "lsjson",
                    "--stat",
                    "--no-mimetype",
                    "--no-modtime",
                    *_rclone_cache_args(),
                    "--",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=rclone_subprocess_env(),
            )
            if result.returncode == 0:
                return True, "ok"
            error = (result.stderr or result.stdout or "").strip()[:500]
            missing = any(
                token in error.lower()
                for token in (
                    "directory not found",
                    "object not found",
                    "path not found",
                )
            )
            if missing and allow_missing:
                return True, "Ziel wird beim ersten Sync angelegt"
            return False, f"rclone lsjson --stat exit={result.returncode}: {error}"
        except subprocess.TimeoutExpired:
            return False, f"Timeout nach {timeout}s"
        except FileNotFoundError:
            return False, "rclone Binary nicht gefunden"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    local = Path(path)
    if local.exists() and local.is_dir():
        return True, "ok"
    if allow_missing and local.parent.exists() and local.parent.is_dir():
        return True, "Ziel wird beim ersten Sync angelegt"
    return False, f"Verzeichnis nicht erreichbar: {path}"


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(amount) < 1024 or unit == "PiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _pair_stats(path: str) -> tuple[int, str]:
    """Ein einzelner rclone-size-Aufruf statt kompletter Doppel-Traversierung."""
    try:
        result = subprocess.run(
            ["rclone", "size", "--json", *_rclone_cache_args(), "--", path],
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode != 0:
            return 0, "?"
        data = json.loads(result.stdout or "{}")
        count = int(data.get("count") or 0)
        size = int(data.get("bytes") or 0)
        return count, _format_bytes(size)
    except Exception as exc:
        logger.warning("Stats für %s nicht verfügbar: %s", path, exc)
        return 0, "?"


def _paths_overlap(a: str, b: str) -> bool:
    return paths_overlap(a, b)


def _has_overlapping_pairs(pairs: list[dict[str, Any]]) -> bool:
    return has_overlapping_pairs(pairs)


def _build_pair_command(
    pair: dict[str, Any],
    base_args: list[str],
    dry_run: bool,
    *,
    config_snapshot: Optional[dict[str, Any]] = None,
) -> tuple[list[str], str, str, str]:
    remote = str(pair["remote"])
    local = str(pair["local"])
    cfg = (
        _SnapshotConfig(config_snapshot)
        if config_snapshot is not None
        else get_config()
    )
    backup = cfg.get("backup", default={}) or {}

    direction = str(pair.get("direction") or "bisync").lower().strip()
    mode = str(pair.get("mode") or "bisync").lower().strip()
    if direction == "bisync":
        verb, src, dst, mode = "bisync", remote, local, "bisync"
    elif direction == "pull":
        verb, src, dst = ("sync" if mode == "sync" else "copy"), remote, local
    elif direction == "push":
        verb, src, dst = ("sync" if mode == "sync" else "copy"), local, remote
    else:
        raise ValueError(f"Unbekannte direction: {direction}")

    allow_unsafe = bool(backup.get("allow_unsafe_rclone_args", False))
    effective_args = validate_parsed_rclone_args(
        list(base_args), allow_unsafe=allow_unsafe
    )
    effective_args += _global_verb_args(cfg, verb)
    effective_args += _structured_rclone_args(backup.get("tuning") or {}, verb=verb)
    effective_args += _structured_rclone_args(pair, verb=verb)
    effective_args += _filter_args(cfg, pair, verb)
    effective_args += _parse_rclone_args(
        pair.get("rclone_args"), allow_unsafe=allow_unsafe
    )
    effective_args += _backup_dir_args(cfg, pair, verb, src, dst)

    if verb in {"sync", "bisync"} and not dry_run:
        if backup.get("require_delete_confirmation", True) and not pair.get(
            "allow_delete", False
        ):
            raise ValueError(
                f"Produktiver {verb} ist gesperrt: Pair-Option allow_delete fehlt"
            )
        effective_max_delete = pair.get(
            "max_delete", (backup.get("tuning") or {}).get("max_delete")
        )
        if backup.get("require_max_delete_for_sync", True) and effective_max_delete in (
            None,
            "",
            -1,
            "-1",
        ):
            raise ValueError(
                f"Produktiver {verb} ist gesperrt: begrenztes max_delete fehlt"
            )

    stats_interval = str((backup.get("tuning") or {}).get("stats_interval") or "10s")
    cmd = [
        "rclone",
        verb,
        *_rclone_cache_args(verb),
        "--stats",
        stats_interval,
        "--stats-one-line",
    ]
    cmd += effective_args
    if dry_run and "--dry-run" not in cmd:
        cmd.append("--dry-run")
    cmd += ["--", src, dst]
    return cmd, verb, direction, mode


def _nearest_existing_path(path: Path) -> Optional[Path]:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else None


def _local_endpoint_fields(pair: dict[str, Any]) -> list[tuple[str, Path]]:
    endpoints: list[tuple[str, Path]] = []
    for field in ("remote", "local"):
        value = str(pair.get(field) or "")
        if value and not _is_remote(value):
            endpoints.append((field, Path(value)))
    return endpoints


def _endpoint_guard_settings(
    pair: dict[str, Any], field: str, path: Path
) -> dict[str, Any]:
    direction = str(pair.get("direction") or "bisync").lower()
    if field == "remote":
        # Mountpoint und Sentinel sind laut Schema relativ zum kanonischen
        # ``local``-Pfad. Ein zweiter lokaler Endpunkt darf diese Guards nicht
        # versehentlich erben; für ihn gilt weiterhin min_remote_files.
        require_mountpoint = False
        mountpoint = path
        sentinel = ""
        min_files = pair.get("min_remote_files", 0)
        destination = direction in {"push", "bisync"}
    else:
        require_mountpoint = bool(pair.get("require_mountpoint", False))
        mountpoint = Path(str(pair.get("mountpoint") or path))
        sentinel = str(pair.get("sentinel_file") or "").strip()
        min_files = pair.get("min_local_files", 1)
        destination = direction in {"pull", "bisync"}
    try:
        min_files_value = max(0, int(min_files if min_files is not None else 0))
    except (TypeError, ValueError):
        min_files_value = 0 if field == "remote" else 1
    if field == "remote":
        check_min_files = direction in {"pull", "bisync"} or min_files_value > 0
    else:
        check_min_files = True
    return {
        "require_mountpoint": require_mountpoint,
        "mountpoint": mountpoint,
        "sentinel": sentinel,
        "min_files": min_files_value,
        "check_min_files": check_min_files,
        "destination": destination,
    }


def _check_local_endpoint(
    pair: dict[str, Any],
    field: str,
    path: Path,
    *,
    include_counts: bool,
    include_free_space: bool,
) -> tuple[bool, str]:
    settings = _endpoint_guard_settings(pair, field, path)
    label = "Remote-Lokalpfad" if field == "remote" else "Lokaler Pfad"
    require_mountpoint = bool(settings["require_mountpoint"])
    mountpoint = settings["mountpoint"]

    if require_mountpoint:
        if not mountpoint.exists() or not os.path.ismount(mountpoint):
            return False, f"Erwarteter Mountpoint ist nicht eingehängt: {mountpoint}"
        try:
            path.resolve().relative_to(mountpoint.resolve())
        except (ValueError, OSError, RuntimeError):
            return False, f"{label} liegt nicht unter dem Mountpoint: {mountpoint}"

    sentinel = str(settings["sentinel"])
    if sentinel and not (path / sentinel).is_file():
        return False, f"Sentinel-Datei fehlt: {path / sentinel}"

    if include_free_space and settings["destination"]:
        try:
            min_free_gb = max(0.0, float(pair.get("min_free_gb", 0) or 0))
        except (TypeError, ValueError):
            min_free_gb = 0.0
        if min_free_gb > 0:
            usage_target = _nearest_existing_path(path)
            if usage_target is None:
                return False, f"Freier Speicher konnte für {path} nicht bestimmt werden"
            free_gb = shutil.disk_usage(usage_target).free / (1024**3)
            if free_gb < min_free_gb:
                return (
                    False,
                    f"Zu wenig freier Speicher unter {usage_target}: "
                    f"{free_gb:.1f} GiB < {min_free_gb:.1f} GiB",
                )

    min_files = int(settings["min_files"])
    if include_counts and settings["check_min_files"] and min_files > 0:
        if not path.exists():
            setting_name = (
                "min_remote_files" if field == "remote" else "min_local_files"
            )
            return False, (
                f"{label} fehlt: {path}. Für ein bewusst neues Ziel "
                f"{setting_name}=0 setzen; andernfalls wird ein fehlender Mount vermutet."
            )
        count = _count_files_up_to(path, min_files)
        if count < min_files:
            setting_name = (
                "min_remote_files" if field == "remote" else "min_local_files"
            )
            return False, (
                f"Nur {count} Dateien unter {path}, {setting_name}={min_files}; "
                "Mount-Drop vermutet."
            )
    return True, "ok"


def _path_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    return {
        "resolved": str(resolved),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def _capture_local_endpoint_guards(
    pair: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    guards: dict[str, dict[str, Any]] = {}
    for field, path in _local_endpoint_fields(pair):
        settings = _endpoint_guard_settings(pair, field, path)
        guard: dict[str, Any] = {
            "path": str(path),
            "identity": _path_identity(path),
        }
        if settings["require_mountpoint"]:
            guard["mountpoint_identity"] = _path_identity(settings["mountpoint"])
        sentinel = str(settings["sentinel"])
        if sentinel:
            guard["sentinel_identity"] = _path_identity(path / sentinel)
        guards[field] = guard
    return guards


def _prepare_local_destinations(pair: dict[str, Any]) -> None:
    for field, path in _local_endpoint_fields(pair):
        settings = _endpoint_guard_settings(pair, field, path)
        if settings["destination"] and not path.exists():
            path.mkdir(parents=True, exist_ok=True)


def _recheck_local_endpoint_guards(
    pair: dict[str, Any], guards: dict[str, dict[str, Any]]
) -> tuple[bool, str]:
    for field, path in _local_endpoint_fields(pair):
        ok, message = _check_local_endpoint(
            pair,
            field,
            path,
            include_counts=False,
            include_free_space=False,
        )
        if not ok:
            return False, message
        guard = guards.get(field)
        if guard is None:
            return False, f"Kein Identitäts-Snapshot für {field} vorhanden"
        try:
            if _path_identity(path) != guard["identity"]:
                return False, f"Pfadidentität hat sich geändert: {path}"
            settings = _endpoint_guard_settings(pair, field, path)
            if settings["require_mountpoint"] and _path_identity(
                settings["mountpoint"]
            ) != guard.get("mountpoint_identity"):
                return False, (
                    f"Mountpoint-Identität hat sich geändert: {settings['mountpoint']}"
                )
            sentinel = str(settings["sentinel"])
            if sentinel and _path_identity(path / sentinel) != guard.get(
                "sentinel_identity"
            ):
                return False, f"Sentinel-Identität hat sich geändert: {path / sentinel}"
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            return False, f"Pfadidentität konnte nicht bestätigt werden: {exc}"
    return True, "ok"


def _remote_file_count(path: str, timeout: int = 120) -> tuple[Optional[int], str]:
    try:
        result = subprocess.run(
            ["rclone", "size", "--json", *_rclone_cache_args(), "--", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode != 0:
            return None, (
                result.stderr or result.stdout or f"exit {result.returncode}"
            ).strip()[:500]
        data = json.loads(result.stdout or "{}")
        return int(data.get("count") or 0), "ok"
    except subprocess.TimeoutExpired:
        return None, f"Timeout nach {timeout}s"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _precheck_pair(pair: dict[str, Any]) -> tuple[bool, str]:
    remote = str(pair.get("remote") or "")
    local = str(pair.get("local") or "")
    direction = str(pair.get("direction") or "bisync").lower()

    remote_is_destination = direction == "push"
    local_is_destination = direction == "pull"
    remote_ok, remote_msg = _remote_reachable(
        remote, allow_missing=remote_is_destination
    )
    local_ok, local_msg = _remote_reachable(local, allow_missing=local_is_destination)
    if not remote_ok or not local_ok:
        return (
            False,
            f"Pre-Check fehlgeschlagen (remote: {remote_msg} / local: {local_msg})",
        )

    min_remote_files = max(0, int(pair.get("min_remote_files", 0) or 0))
    if min_remote_files > 0 and direction in {"pull", "bisync"} and _is_remote(remote):
        remote_count, remote_count_msg = _remote_file_count(remote)
        if remote_count is None:
            return (
                False,
                f"Remote-Dateischutz konnte nicht geprüft werden: {remote_count_msg}",
            )
        if remote_count < min_remote_files:
            return (
                False,
                f"Nur {remote_count} Dateien unter {remote}, min_remote_files={min_remote_files}.",
            )

    for field, path in _local_endpoint_fields(pair):
        endpoint_ok, endpoint_message = _check_local_endpoint(
            pair,
            field,
            path,
            include_counts=True,
            include_free_space=True,
        )
        if not endpoint_ok:
            return False, endpoint_message
    return True, "ok"


def parse_final_stats(log_tail: str) -> dict[str, Any]:
    """Parst die letzte rclone-'Transferred:'-Zeile aus einem Log-Ausschnitt."""
    for line in reversed(log_tail.splitlines()):
        match = _STATS_RE.search(line)
        if match:
            return {
                "transferred": match.group(1).strip() if match.group(1) else None,
                "total": match.group(2).strip() if match.group(2) else None,
                "percent": float(match.group(3)) if match.group(3) else None,
                "speed": match.group(4).strip() if match.group(4) else None,
                "eta": match.group(5),
            }
    return {}


def _first_run_resync_allowed(
    pair: dict[str, Any], backup: dict[str, Any], pair_name: str
) -> bool:
    """Erlaubt den Baseline-Resync genau einmal: solange das Pair noch nie
    erfolgreich gelaufen ist. Danach ist ein Resync-Verlangen ein Störfall,
    der bewusst per auto_resync freigegeben werden muss."""
    if not bool(
        pair.get("auto_resync_first_run", backup.get("auto_resync_first_run", True))
    ):
        return False
    try:
        from ..db import get_db

        state = get_db().pair_baseline_state(
            pair_name,
            history_key=rclone_history_key(pair),
        )
        if state == "new":
            return True
        if state == "ambiguous":
            logger.warning(
                "[%s] Baseline-Historie ist nicht eindeutig; automatischer "
                "Erststart-Resync bleibt gesperrt",
                pair_name,
            )
        return False
    except Exception:
        logger.exception(
            "[%s] Erststart-Prüfung fehlgeschlagen; Resync bleibt gesperrt", pair_name
        )
        return False


def _sync_pair(
    pair: dict[str, Any],
    args: list[str],
    log_dir: Path,
    dry_run: bool,
    timeout_sec: int,
    run_id: str,
    config_snapshot: dict[str, Any],
) -> dict[str, Any]:
    name = str(pair["name"])
    remote = str(pair["remote"])
    local = str(pair["local"])
    summary: dict[str, Any] = {
        "name": name,
        "remote": remote,
        "local": local,
        "log_file": "",
        "ok": False,
        "error": "",
        "dry_run": dry_run,
    }

    runtime_state.update_pair(run_id, name, "checking")
    ok, message = _precheck_pair(pair)
    if not ok:
        logger.error("[%s] %s", name, message)
        summary.update({"error": message, "skipped": True})
        runtime_state.update_pair(run_id, name, "error", error=message)
        try:
            from ..notifications import notify

            notify(
                "mount_check_failed", f"Pair '{name}' abgebrochen", message, pair=name
            )
        except Exception:
            pass
        return summary

    try:
        _prepare_local_destinations(pair)
        endpoint_guards = _capture_local_endpoint_guards(pair)
    except (OSError, RuntimeError, ValueError) as exc:
        summary.update(
            {
                "error": f"Lokale Pfadidentität konnte nicht fixiert werden: {exc}",
                "skipped": True,
            }
        )
        runtime_state.update_pair(run_id, name, "error", error=summary["error"])
        return summary

    log_file = (
        log_dir / f"sync-{_safe_name(name)}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    )
    summary["log_file"] = str(log_file)
    _set_active_pair_log(name, log_file)
    runtime_state.update_pair(
        run_id, name, "running", log_file=str(log_file), started_at=time.time()
    )

    backup = config_snapshot.get("backup") or {}
    collect_stats = bool(backup.get("collect_pre_post_stats", False))
    if collect_stats:
        cloud_files, cloud_size = _pair_stats(remote)
        local_files_before, local_size_before = _pair_stats(local)
        summary.update(
            {
                "cloud_files": cloud_files,
                "cloud_size": cloud_size,
                "local_files_before": local_files_before,
                "local_size_before": local_size_before,
            }
        )

    try:
        cmd, verb, direction, mode = _build_pair_command(
            pair, args, dry_run, config_snapshot=config_snapshot
        )
        summary.update(
            {
                "verb": verb,
                "direction": direction,
                "mode": mode,
                "command": command_to_string(cmd),
            }
        )
        logger.info("[%s] [%s/%s] %s", name, direction, mode, summary["command"])

        if is_cancelled():
            summary["error"] = "Vor Start abgebrochen"
            summary["cancelled"] = True
            runtime_state.update_pair(run_id, name, "cancelled", error=summary["error"])
            return summary

        rc = _run_rclone_command(
            cmd,
            log_file,
            timeout_sec=timeout_sec,
            pair_name=name,
            run_id=run_id,
            pre_spawn_check=lambda: _recheck_local_endpoint_guards(
                pair, endpoint_guards
            ),
        )
        log_tail = read_log_tail(log_file)
        needs_resync = (
            verb == "bisync" and rc != 0 and bool(_RESYNC_RE.search(log_tail))
        )
        auto_resync = bool(pair.get("auto_resync", backup.get("auto_resync", False)))
        first_run_resync = False
        if needs_resync and not auto_resync:
            first_run_resync = _first_run_resync_allowed(pair, backup, name)
        if needs_resync and (auto_resync or first_run_resync) and not is_cancelled():
            mode_value = (
                str(pair.get("resync_mode") or backup.get("resync_mode") or "")
                .strip()
                .lower()
            )
            separator_index = cmd.index("--")
            resync_flags = ["--resync"]
            if mode_value and mode_value != "path1":
                resync_flags += ["--resync-mode", mode_value]
            resync_cmd = [*cmd[:separator_index], *resync_flags, *cmd[separator_index:]]
            if first_run_resync:
                logger.warning(
                    "[%s] Erstlauf: Baseline-Resync wird automatisch ausgeführt", name
                )
                summary["resync"] = "first_run"
            else:
                logger.warning("[%s] automatischer Resync wird ausgeführt", name)
                summary["resync"] = "auto"
            rc = _run_rclone_command(
                resync_cmd,
                log_file,
                timeout_sec=timeout_sec,
                append=True,
                header="\n\n=== AUTO RESYNC ===\n\n",
                pair_name=name,
                run_id=run_id,
                pre_spawn_check=lambda: _recheck_local_endpoint_guards(
                    pair, endpoint_guards
                ),
            )
            summary["resync_return_code"] = rc
            log_tail = read_log_tail(log_file)
        elif needs_resync:
            summary["resync_required"] = True

        summary["return_code"] = rc
        summary.update(parse_final_stats(log_tail))
        summary["cancelled"] = is_cancelled() or rc == 130
        summary["ok"] = rc == 0 and not summary["cancelled"]
        if not summary["ok"]:
            if summary["cancelled"]:
                summary["error"] = "Abgebrochen"
            elif summary.get("resync_required"):
                summary["error"] = "rclone verlangt einen geprüften --resync-Lauf"
            else:
                summary["error"] = f"rclone exit {rc}"

        if collect_stats:
            local_files_after, local_size_after = _pair_stats(local)
            summary["local_files_after"] = local_files_after
            summary["local_size_after"] = local_size_after
            if (
                summary.get("cloud_files")
                and local_files_after
                and direction == "bisync"
                and summary["cloud_files"] != local_files_after
            ):
                summary["warning"] = "Cloud/Lokal-Dateianzahl unterschiedlich"

        state = (
            "done"
            if summary["ok"]
            else ("cancelled" if summary["cancelled"] else "error")
        )
        runtime_state.update_pair(
            run_id, name, state, ok=summary["ok"], error=summary.get("error", "")
        )
        return summary
    except subprocess.TimeoutExpired:
        summary["error"] = f"Timeout nach {round(timeout_sec / 3600, 1)}h"
        logger.error("[%s] Timeout", name)
        runtime_state.update_pair(run_id, name, "error", error=summary["error"])
        return summary
    except Exception as exc:
        summary["error"] = str(exc)
        logger.exception("[%s] Exception", name)
        runtime_state.update_pair(run_id, name, "error", error=summary["error"])
        return summary
    finally:
        _set_active_pair_log(name, None)


def _selected_pairs(
    pairs: Iterable[dict[str, Any]], pairs_filter: Optional[list[str]]
) -> list[dict[str, Any]]:
    selected = [pair for pair in pairs if pair.get("enabled", True)]
    if pairs_filter:
        wanted = {str(name).strip() for name in pairs_filter if str(name).strip()}
        selected = [pair for pair in selected if pair.get("name") in wanted]
    return selected


def _next_runnable_pair_index(
    pending: list[dict[str, Any]], running: Iterable[dict[str, Any]]
) -> Optional[int]:
    active = list(running)
    for index, candidate in enumerate(pending):
        if not any(pairs_conflict(candidate, item) for item in active):
            return index
    return None


def run_job(
    dry_run: bool = False,
    pairs_filter: Optional[list[str]] = None,
    *,
    trigger: str = "manual",
    job_id: int | None = None,
    defer_runtime_finish: bool = False,
    reset_cancel_state: bool = True,
) -> dict[str, Any]:
    trigger = str(trigger or "manual").strip().lower()[:32] or "manual"
    cfg = get_config()
    config_snapshot = cfg.snapshot()
    backup = config_snapshot.get("backup") or {}
    if not backup.get("enabled", True):
        return {"enabled": False, "ok": True}

    pairs = _selected_pairs(backup.get("pairs") or [], pairs_filter)
    if not pairs:
        return {
            "enabled": True,
            "ok": False,
            "error": "Keine aktiven Sync-Paare passend zur Auswahl",
        }

    names = [str(pair.get("name") or "").strip() for pair in pairs]
    if any(not name for name in names):
        return {
            "enabled": True,
            "ok": False,
            "error": "Aktives Pair ohne Namen gefunden",
        }
    folded_names = [name.casefold() for name in names]
    if len(set(folded_names)) != len(folded_names):
        return {
            "enabled": True,
            "ok": False,
            "error": "Doppelte aktive Pair-Namen gefunden",
        }

    # Alles, was vor einem Lauf fehlschlagen kann, vor dem Laufzeit-Marker prüfen.
    base_args = _parse_rclone_args(
        backup.get("rclone_args"),
        allow_unsafe=bool(backup.get("allow_unsafe_rclone_args", False)),
    )
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    )
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass
    timeout_sec = int(
        _bounded_number(
            backup.get("timeout_hours", 4), default=4, minimum=0.1, maximum=168
        )
        * 3600
    )
    max_parallel = int(
        _bounded_number(backup.get("max_parallel", 2), default=2, minimum=1, maximum=16)
    )
    max_parallel = min(max_parallel, len(pairs))

    if reset_cancel_state:
        reset_cancel()
    run_id = runtime_state.begin_run(names, dry_run=dry_run, job_id=job_id)
    warnings: list[str] = []

    if not dry_run:
        try:
            from ..notifications import notify

            notify(
                "sync_started",
                "Sync gestartet",
                f"{len(pairs)} Pair(s): {', '.join(names)}",
            )
        except Exception:
            logger.exception("Start-Benachrichtigung fehlgeschlagen")
    if _has_overlapping_pairs(pairs):
        warnings.append(
            "Überlappende Pair-Pfade erkannt; nur aktuell kollidierende Pairs "
            "werden serialisiert."
        )

    logger.info("Starte Sync mit %d Worker(n) für %d Pair(s)", max_parallel, len(pairs))
    started = time.time()
    results_by_name: dict[str, dict[str, Any]] = {}
    try:
        with ThreadPoolExecutor(
            max_workers=max_parallel, thread_name_prefix="rclone-pair"
        ) as executor:
            pending = list(pairs)
            running: dict[Future[dict[str, Any]], dict[str, Any]] = {}
            while pending or running:
                if is_cancelled() and pending:
                    for pair in pending:
                        name = str(pair.get("name") or "?")
                        result = {
                            "name": name,
                            "ok": False,
                            "cancelled": True,
                            "skipped": True,
                            "error": "Vor Start abgebrochen",
                            "trigger": trigger,
                        }
                        runtime_state.update_pair(
                            run_id, name, "cancelled", error=result["error"]
                        )
                        results_by_name[name] = result
                    pending.clear()

                while pending and len(running) < max_parallel:
                    runnable_index = _next_runnable_pair_index(
                        pending, running.values()
                    )
                    if runnable_index is None:
                        break
                    pair = pending.pop(runnable_index)
                    future = executor.submit(
                        _sync_pair,
                        pair,
                        base_args,
                        log_dir,
                        dry_run,
                        timeout_sec,
                        run_id,
                        config_snapshot,
                    )
                    running[future] = pair

                if not running:
                    continue
                done, _not_done = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    pair = running.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.exception("Pair-Worker fehlgeschlagen")
                        result = {
                            "name": pair.get("name", "?"),
                            "ok": False,
                            "error": str(exc),
                        }
                    result.setdefault("trigger", trigger)
                    results_by_name[str(pair.get("name"))] = result
    except BaseException as exc:
        if not defer_runtime_finish:
            runtime_state.finish_run(run_id, "error", error=str(exc))
        raise

    results = [
        results_by_name.get(
            str(pair.get("name")),
            {"name": pair.get("name"), "ok": False, "error": "Kein Ergebnis"},
        )
        for pair in pairs
    ]
    for pair, result in zip(pairs, results):
        result.setdefault("history_key", rclone_history_key(pair))
    duration = time.time() - started
    ok_count = sum(1 for result in results if result.get("ok"))
    cancelled = is_cancelled()
    summary: dict[str, Any] = {
        "enabled": True,
        "ok": ok_count == len(pairs) and not cancelled,
        "started_at": datetime.fromtimestamp(started).isoformat(),
        "duration_sec": round(duration, 1),
        "dry_run": dry_run,
        "pairs": results,
        "ok_count": ok_count,
        "total_pairs": len(pairs),
        "cancelled": cancelled,
        "warnings": warnings,
        "run_id": run_id,
        "trigger": trigger,
    }

    if not defer_runtime_finish:
        runtime_state.finish_run(
            run_id, "cancelled" if cancelled else ("ok" if summary["ok"] else "error")
        )
    if not dry_run:
        try:
            from ..notifications import notify

            if summary["ok"]:
                notify(
                    "sync_ok",
                    f"Sync erfolgreich ({ok_count}/{len(pairs)} Paare)",
                    f"Dauer: {duration:.0f}s",
                    summary=summary,
                )
            elif not cancelled:
                failed = [
                    str(result.get("name", "?"))
                    for result in results
                    if not result.get("ok")
                ]
                notify(
                    "sync_error",
                    f"Sync mit Fehlern ({ok_count}/{len(pairs)})",
                    f"Fehlgeschlagen: {', '.join(failed)}\nDauer: {duration:.0f}s",
                    summary=summary,
                )
        except Exception as exc:
            logger.warning("Benachrichtigung fehlgeschlagen: %s", exc)
    return summary


def build_job_plan(
    dry_run: bool = True, pairs_filter: Optional[list[str]] = None
) -> dict[str, Any]:
    cfg = get_config()
    backup = cfg.get("backup", default={}) or {}
    pairs = _selected_pairs(backup.get("pairs") or [], pairs_filter)
    base_args = _parse_rclone_args(
        backup.get("rclone_args"),
        allow_unsafe=bool(backup.get("allow_unsafe_rclone_args", False)),
    )
    planned: list[dict[str, Any]] = []
    warnings: list[str] = []
    if _has_overlapping_pairs(pairs):
        warnings.append(
            "Überlappende Pair-Pfade: produktive Läufe werden automatisch seriell ausgeführt."
        )
    for pair in pairs:
        try:
            cmd, verb, direction, mode = _build_pair_command(
                pair, base_args, dry_run=dry_run
            )
            pair_warnings = _pair_warnings(pair, dry_run=dry_run)
            warnings.extend(pair_warnings)
            planned.append(
                {
                    "name": pair.get("name"),
                    "enabled": pair.get("enabled", True),
                    "remote": pair.get("remote"),
                    "local": pair.get("local"),
                    "verb": verb,
                    "direction": direction,
                    "mode": mode,
                    "dry_run": dry_run,
                    "command": command_to_string(cmd),
                    "warnings": pair_warnings,
                }
            )
        except Exception as exc:
            planned.append({"name": pair.get("name"), "ok": False, "error": str(exc)})
            warnings.append(
                f"{pair.get('name', '?')}: Plan konnte nicht gebaut werden: {exc}"
            )
    return {
        "ok": not any(item.get("error") for item in planned),
        "dry_run": dry_run,
        "total_pairs": len(planned),
        "pairs": planned,
        "warnings": list(dict.fromkeys(warnings)),
    }


def run_pair_check(
    pair_name: str,
    *,
    one_way: Optional[bool] = None,
    download: bool = False,
    reset_cancel_state: bool = True,
) -> dict[str, Any]:
    cfg = get_config()
    backup = cfg.get("backup", default={}) or {}
    matches = [
        pair for pair in (backup.get("pairs") or []) if pair.get("name") == pair_name
    ]
    if not matches:
        return {"ok": False, "error": f"Pair nicht gefunden: {pair_name}"}
    pair = matches[0]
    precheck_ok, precheck_message = _precheck_pair(pair)
    if not precheck_ok:
        return {
            "ok": False,
            "skipped": True,
            "error": precheck_message,
            "pair": pair_name,
        }
    try:
        endpoint_guards = _capture_local_endpoint_guards(pair)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "skipped": True,
            "error": f"Lokale Pfadidentität konnte nicht fixiert werden: {exc}",
            "pair": pair_name,
        }
    direction = str(pair.get("direction") or "bisync").lower().strip()
    src, dst = (
        (pair.get("local"), pair.get("remote"))
        if direction == "push"
        else (pair.get("remote"), pair.get("local"))
    )
    if one_way is None:
        one_way = direction in {"pull", "push"}

    if reset_cancel_state:
        reset_cancel()
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = (
        log_dir / f"check-{_safe_name(pair_name)}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    )
    args = _filter_args(cfg, pair, "check")
    args += _parse_rclone_args(
        backup.get("check_args"),
        allow_unsafe=bool(backup.get("allow_unsafe_rclone_args", False)),
    )
    bwlimit = str(
        backup.get("bwlimit") or (backup.get("tuning") or {}).get("bwlimit") or ""
    ).strip()
    if bwlimit:
        args += ["--bwlimit", bwlimit]
    cmd = [
        "rclone",
        "check",
        *_rclone_cache_args(),
        "--stats",
        "10s",
        "--stats-one-line",
        *args,
    ]
    if one_way:
        cmd.append("--one-way")
    if download:
        cmd.append("--download")
    cmd += ["--", str(src), str(dst)]
    timeout_sec = max(300, int(float(backup.get("timeout_hours", 4) or 4) * 3600))
    result: dict[str, Any] = {
        "pair": pair_name,
        "src": src,
        "dst": dst,
        "one_way": one_way,
        "download": download,
        "log_file": str(log_file),
        "command": command_to_string(cmd),
    }
    try:
        rc = _run_rclone_command(
            cmd,
            log_file,
            timeout_sec=timeout_sec,
            pair_name=f"check:{pair_name}",
            pre_spawn_check=lambda: _recheck_local_endpoint_guards(
                pair, endpoint_guards
            ),
        )
        result.update(
            {
                "return_code": rc,
                "ok": rc == 0 and not is_cancelled(),
                "cancelled": is_cancelled(),
            }
        )
        result.update(parse_final_stats(read_log_tail(log_file)))
        if not result["ok"]:
            result["error"] = (
                "Abgebrochen" if result["cancelled"] else f"rclone check exit {rc}"
            )
    except subprocess.TimeoutExpired:
        result.update(
            {"ok": False, "error": f"Timeout nach {round(timeout_sec / 3600, 1)}h"}
        )
    except Exception as exc:
        logger.exception("Pair-Check fehlgeschlagen")
        result.update({"ok": False, "error": str(exc)})
    return result


def run_quick(
    remote_path: str,
    local_path: str,
    direction: str = "bisync",
    mode: str = "bisync",
    dry_run: bool = False,
    extra_args: Optional[list[str] | str] = None,
    allow_delete: bool = False,
    max_delete: Optional[int] = None,
    min_local_files: int = 1,
    reset_cancel_state: bool = True,
) -> dict[str, Any]:
    cfg = get_config()
    try:
        min_local_files_i = max(0, int(min_local_files))
    except (TypeError, ValueError):
        min_local_files_i = 1
    pair = {
        "name": "quick",
        "remote": remote_path,
        "local": local_path,
        "direction": direction,
        "mode": mode,
        "min_local_files": min_local_files_i,
        "allow_delete": bool(allow_delete),
        "max_delete": max_delete,
    }
    roots = [
        Path(str(value)).expanduser().resolve()
        for value in (
            cfg.get(
                "web",
                "local_browse_roots",
                default=["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
            )
            or []
        )
        if str(value).strip()
    ]
    try:
        for field, path in _local_endpoint_fields(pair):
            resolved = path.expanduser().resolve()
            if roots and not any(
                resolved == root or resolved.is_relative_to(root) for root in roots
            ):
                raise ValueError(
                    f"Quick-Sync-{field} liegt außerhalb erlaubter Wurzeln: {resolved}"
                )
            pair[field] = str(resolved)
        remote_path = str(pair["remote"])
        local_path = str(pair["local"])
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
            "skipped": True,
            "remote": remote_path,
            "local": local_path,
        }
    ok, message = _precheck_pair(pair)
    if not ok:
        return {
            "ok": False,
            "error": message,
            "skipped": True,
            "remote": remote_path,
            "local": local_path,
        }

    # Bewusst neue lokale Ziele werden erst nach bestandenem Precheck angelegt.
    try:
        _prepare_local_destinations(pair)
        endpoint_guards = _capture_local_endpoint_guards(pair)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "error": f"Lokale Pfadidentität konnte nicht fixiert werden: {exc}",
            "skipped": True,
            "remote": remote_path,
            "local": local_path,
        }

    if reset_cancel_state:
        reset_cancel()
    log_dir = (
        Path(cfg.get("paths", "logs_dir", default="/opt/rclone-sync/logs")) / "rclone"
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(f"{remote_path}-{local_path}", "quick")
    log_file = log_dir / f"quick-{safe}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    backup = cfg.get("backup", default={}) or {}
    allow_unsafe = bool(backup.get("allow_unsafe_rclone_args", False))
    base_args = _parse_rclone_args(
        backup.get("rclone_args", []), allow_unsafe=allow_unsafe
    )
    if extra_args:
        base_args += _parse_rclone_args(extra_args, allow_unsafe=allow_unsafe)
    summary: dict[str, Any] = {
        "direction": direction,
        "mode": mode,
        "remote": remote_path,
        "local": local_path,
        "dry_run": dry_run,
        "log_file": str(log_file),
    }
    try:
        cmd, verb, direction, mode = _build_pair_command(pair, base_args, dry_run)
        summary.update(
            {
                "command": command_to_string(cmd),
                "verb": verb,
                "direction": direction,
                "mode": mode,
            }
        )
        timeout_sec = max(
            300,
            int(
                float(
                    (cfg.get("backup", default={}) or {}).get("timeout_hours", 4) or 4
                )
                * 3600
            ),
        )
        rc = _run_rclone_command(
            cmd,
            log_file,
            timeout_sec=timeout_sec,
            pair_name="quick",
            pre_spawn_check=lambda: _recheck_local_endpoint_guards(
                pair, endpoint_guards
            ),
        )
        tail = read_log_tail(log_file)
        needs_resync = verb == "bisync" and rc != 0 and bool(_RESYNC_RE.search(tail))
        if needs_resync:
            summary["resync_required"] = True
        summary.update(
            {
                "return_code": rc,
                "cancelled": is_cancelled(),
                "ok": rc == 0 and not is_cancelled(),
            }
        )
        summary.update(parse_final_stats(tail))
        if not summary["ok"]:
            summary["error"] = (
                "Abgebrochen"
                if summary["cancelled"]
                else (
                    "rclone verlangt --resync" if needs_resync else f"rclone exit {rc}"
                )
            )
    except subprocess.TimeoutExpired:
        summary.update({"ok": False, "error": "Timeout"})
    except Exception as exc:
        logger.exception("Quick-Sync fehlgeschlagen")
        summary.update({"ok": False, "error": str(exc)})
    return summary
