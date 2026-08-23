"""Prozessübergreifender Laufzeitstatus und Cancel-Signal."""

from __future__ import annotations

import json
import os
import re
import signal
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

STATE_DIR = Path(os.getenv("RCLONE_SYNC_STATE_DIR", "/opt/rclone-sync/data/runtime"))
RUN_FILE = STATE_DIR / "current-run.json"
CANCEL_FILE = STATE_DIR / "cancel.requested"
PROCS_DIR = STATE_DIR / "processes"

_state_lock = threading.RLock()
_local_process_markers: dict[int, str] = {}
DEFAULT_CANCEL_SCOPE = "backup"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ProcessTerminationError(OSError):
    """Ein Prozessabbruch konnte nicht sicher bis zum Exit bestätigt werden."""


def _scope_name(scope: str | None) -> str:
    value = str(scope or DEFAULT_CANCEL_SCOPE).strip().lower()
    if not _SCOPE_RE.fullmatch(value):
        raise ValueError("Ungültiger Runtime-Scope")
    return value


def _cancel_path(scope: str | None = DEFAULT_CANCEL_SCOPE) -> Path:
    normalized = _scope_name(scope)
    if normalized == DEFAULT_CANCEL_SCOPE:
        # Der historische Pfad bleibt für laufende Installationen und externe
        # Werkzeuge kompatibel. Weitere Job-Arten erhalten getrennte Marker.
        return CANCEL_FILE
    return STATE_DIR / f"cancel.{normalized}.requested"


def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROCS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (STATE_DIR, PROCS_DIR):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dirs()
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def reset_cancel_marker(scope: str = DEFAULT_CANCEL_SCOPE) -> None:
    _ensure_dirs()
    _cancel_path(scope).unlink(missing_ok=True)


def request_cancel_marker(scope: str = DEFAULT_CANCEL_SCOPE) -> None:
    """Setzt das Cancel-Signal atomar, ohne vorhandenen Symlinks zu folgen."""
    _ensure_dirs()
    cancel_path = _cancel_path(scope)
    fd = -1
    tmp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{cancel_path.name}.", suffix=".tmp", dir=str(STATE_DIR)
        )
        tmp = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = -1
            handle.write(str(time.time()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, cancel_path)
        tmp = None
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def cancel_requested(scope: str = DEFAULT_CANCEL_SCOPE) -> bool:
    return _cancel_path(scope).exists()


def begin_run(
    pair_names: list[str],
    *,
    dry_run: bool,
    kind: str = "backup",
    job_id: int | None = None,
) -> str:
    run_id = uuid.uuid4().hex
    now = time.time()
    state = {
        "run_id": run_id,
        "kind": kind,
        "pid": os.getpid(),
        "owner_start_ticks": _proc_start_ticks(os.getpid()),
        "dry_run": bool(dry_run),
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "pairs": {
            name: {"name": name, "status": "pending", "updated_at": now}
            for name in pair_names
        },
    }
    if job_id is not None:
        state["job_id"] = int(job_id)
    with _state_lock:
        _atomic_json(RUN_FILE, state)
    return run_id


def update_pair(run_id: str, pair_name: str, status: str, **extra: Any) -> None:
    with _state_lock:
        state = _read_json(RUN_FILE)
        if not state or state.get("run_id") != run_id:
            return
        pairs = state.setdefault("pairs", {})
        current = pairs.setdefault(pair_name, {"name": pair_name})
        current.update({"status": status, "updated_at": time.time(), **extra})
        state["updated_at"] = time.time()
        _atomic_json(RUN_FILE, state)


def finish_run(run_id: str, status: str, **extra: Any) -> None:
    with _state_lock:
        state = _read_json(RUN_FILE)
        if not state or state.get("run_id") != run_id:
            return
        state.update(
            {
                "status": status,
                "ended_at": time.time(),
                "updated_at": time.time(),
                **extra,
            }
        )
        _atomic_json(RUN_FILE, state)


def load_run_state() -> Optional[dict[str, Any]]:
    with _state_lock:
        return _read_json(RUN_FILE)


def _proc_start_ticks(pid: int) -> Optional[str]:
    try:
        # /proc/<pid>/stat Feld 22; Prozessname kann Leerzeichen enthalten,
        # daher erst hinter der letzten schließenden Klammer splitten.
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rest = raw[raw.rfind(")") + 2 :].split()
        return rest[19]
    except (OSError, IndexError):
        return None


def recover_stale_run_details(
    *, reason: str = "Prozess nicht mehr aktiv", force: bool = False
) -> Optional[dict[str, Any]]:
    """Markiert einen verwaisten Lauf stale und gibt dessen Metadaten zurück."""
    with _state_lock:
        state = _read_json(RUN_FILE)
        if not state or state.get("status") != "running":
            return None
        try:
            pid = int(state.get("pid"))
        except (TypeError, ValueError):
            pid = -1
        expected_ticks = state.get("owner_start_ticks")
        alive = pid > 0 and _proc_start_ticks(pid) is not None
        if alive and expected_ticks and _proc_start_ticks(pid) != expected_ticks:
            alive = False
        if alive and not force:
            return None
        now = time.time()
        state.update(
            {"status": "stale", "ended_at": now, "updated_at": now, "error": reason}
        )
        for pair in (state.get("pairs") or {}).values():
            if isinstance(pair, dict) and pair.get("status") in {
                "pending",
                "checking",
                "running",
            }:
                pair.update({"status": "stale", "updated_at": now, "error": reason})
        current = _read_json(RUN_FILE)
        if (
            not current
            or current.get("status") != "running"
            or current.get("run_id") != state.get("run_id")
        ):
            return None
        _atomic_json(RUN_FILE, state)
        run_scope = _scope_name(str(state.get("kind") or DEFAULT_CANCEL_SCOPE))
        run_id = str(state.get("run_id") or "")
        owner_start_ticks = str(state.get("owner_start_ticks") or "")
        for marker_path in PROCS_DIR.glob("*.json"):
            try:
                marker = _read_json(marker_path)
                if not marker:
                    marker_path.unlink(missing_ok=True)
                    continue
                marker_scope = _scope_name(
                    str(marker.get("scope") or DEFAULT_CANCEL_SCOPE)
                )
                marker_owner = int(marker.get("owner_pid") or -1)
                marker_run_id = str(marker.get("run_id") or "")
                marker_owner_ticks = str(marker.get("owner_start_ticks") or "")
                same_run = bool(run_id and marker_run_id == run_id)
                legacy_same_owner = bool(
                    not marker_run_id
                    and marker_owner == pid
                    and owner_start_ticks
                    and marker_owner_ticks == owner_start_ticks
                )
                if marker_scope == run_scope and (same_run or legacy_same_owner):
                    marker_path.unlink(missing_ok=True)
            except OSError:
                pass
            except (TypeError, ValueError):
                marker_path.unlink(missing_ok=True)
        reset_cancel_marker(run_scope)
        return dict(state)


def recover_stale_run_state(*, reason: str = "Prozess nicht mehr aktiv") -> bool:
    """Rückwärtskompatibler Bool-Wrapper für die detaillierte Recovery-API."""
    return recover_stale_run_details(reason=reason) is not None


def register_process(
    pid: int,
    *,
    pair_name: str = "",
    log_file: str = "",
    scope: str = DEFAULT_CANCEL_SCOPE,
    executable: str = "rclone",
    run_id: str = "",
) -> str:
    _ensure_dirs()
    marker_id = uuid.uuid4().hex
    marker = {
        "marker_id": marker_id,
        "pid": int(pid),
        "owner_pid": os.getpid(),
        "owner_start_ticks": _proc_start_ticks(os.getpid()),
        "pair_name": pair_name,
        "log_file": log_file,
        "scope": _scope_name(scope),
        "executable": Path(str(executable or "")).name,
        "run_id": str(run_id or ""),
        "start_ticks": _proc_start_ticks(pid),
        "registered_at": time.time(),
    }
    _atomic_json(PROCS_DIR / f"{pid}-{marker_id}.json", marker)
    with _state_lock:
        _local_process_markers[int(pid)] = marker_id
    return marker_id


def unregister_process(pid: int, *, marker_id: str | None = None) -> None:
    normalized_pid = int(pid)
    with _state_lock:
        known_marker = _local_process_markers.get(normalized_pid)
        if marker_id is None:
            marker_id = known_marker
        if marker_id and known_marker == marker_id:
            _local_process_markers.pop(normalized_pid, None)
    if not marker_id:
        return
    if not re.fullmatch(r"[a-f0-9]{32}", str(marker_id)):
        return
    try:
        (PROCS_DIR / f"{normalized_pid}-{marker_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _is_same_registered_process(marker: dict[str, Any]) -> bool:
    try:
        pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return False
    expected_ticks = str(marker.get("start_ticks") or "")
    if not expected_ticks or _proc_start_ticks(pid) != expected_ticks:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    except OSError:
        return False
    if not cmdline or not cmdline[0]:
        return False
    expected = Path(str(marker.get("executable") or "rclone")).name
    actual = Path(cmdline[0].decode("utf-8", errors="ignore")).name
    return bool(expected and actual == expected)


def active_processes(
    scope: str | None = DEFAULT_CANCEL_SCOPE,
) -> list[dict[str, Any]]:
    _ensure_dirs()
    normalized_scope = _scope_name(scope) if scope is not None else None
    active: list[dict[str, Any]] = []
    for marker_path in PROCS_DIR.glob("*.json"):
        marker = _read_json(marker_path)
        if not marker or not _is_same_registered_process(marker):
            marker_path.unlink(missing_ok=True)
            continue
        try:
            marker_scope = _scope_name(str(marker.get("scope") or DEFAULT_CANCEL_SCOPE))
        except (TypeError, ValueError):
            marker_path.unlink(missing_ok=True)
            continue
        if normalized_scope is None or marker_scope == normalized_scope:
            active.append(marker)
    return active


def terminate_active_processes(
    graceful_sec: int = 10,
    *,
    scope: str = DEFAULT_CANCEL_SCOPE,
    request_cancel: bool = True,
    forceful_sec: float = 5.0,
) -> int:
    """Beendet nur Prozessgruppen des angeforderten Job-Scopes."""
    normalized_scope = _scope_name(scope)
    marker_error: OSError | None = None
    if request_cancel:
        try:
            request_cancel_marker(normalized_scope)
        except OSError as exc:
            # Bekannte Prozesse trotzdem beenden. Der Aufrufer muss danach aber
            # erfahren, dass ein anderer Worker das Cancel-Signal nicht sieht.
            marker_error = exc
    markers = active_processes(normalized_scope)
    killed = 0
    for marker in markers:
        if not _is_same_registered_process(marker):
            continue
        pid = int(marker["pid"])
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue

    def _remaining(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remaining: list[dict[str, Any]] = []
        for candidate in candidates:
            if _is_same_registered_process(candidate):
                remaining.append(candidate)
                continue
            unregister_process(
                int(candidate["pid"]),
                marker_id=str(candidate.get("marker_id") or ""),
            )
        return remaining

    def _wait_for_exit(
        candidates: list[dict[str, Any]], timeout_sec: float
    ) -> list[dict[str, Any]]:
        remaining = _remaining(candidates)
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while remaining and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            remaining = _remaining(remaining)
        # Auch bei Timeout 0 mindestens einmal nach dem Signal prüfen.
        return _remaining(remaining)

    remaining = _wait_for_exit(markers, graceful_sec)
    for marker in remaining:
        if not _is_same_registered_process(marker):
            continue
        pid = int(marker["pid"])
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    remaining = _wait_for_exit(remaining, forceful_sec)
    if remaining:
        pids = ", ".join(str(marker.get("pid")) for marker in remaining)
        message = (
            "Prozess-Exit nach SIGKILL nicht bestätigt; "
            f"Marker bleiben erhalten (PID(s): {pids})"
        )
        if marker_error is not None:
            message = f"{marker_error}; {message}"
        raise ProcessTerminationError(message) from marker_error
    if marker_error is not None:
        raise marker_error
    return killed
