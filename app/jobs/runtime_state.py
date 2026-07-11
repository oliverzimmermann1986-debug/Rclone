"""Prozessübergreifender Laufzeitstatus und Cancel-Signal."""

from __future__ import annotations

import json
import os
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
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
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


def reset_cancel_marker() -> None:
    _ensure_dirs()
    CANCEL_FILE.unlink(missing_ok=True)


def request_cancel_marker() -> None:
    """Setzt das Cancel-Signal atomar, ohne vorhandenen Symlinks zu folgen."""
    _ensure_dirs()
    fd = -1
    tmp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{CANCEL_FILE.name}.", suffix=".tmp", dir=str(STATE_DIR)
        )
        tmp = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = -1
            handle.write(str(time.time()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CANCEL_FILE)
        tmp = None
    except OSError:
        pass
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def cancel_requested() -> bool:
    return CANCEL_FILE.exists()


def begin_run(pair_names: list[str], *, dry_run: bool, kind: str = "backup") -> str:
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


def recover_stale_run_state(*, reason: str = "Prozess nicht mehr aktiv") -> bool:
    """Markiert einen zurückgelassenen Laufzeitstatus nach Absturz als stale."""
    with _state_lock:
        state = _read_json(RUN_FILE)
        if not state or state.get("status") != "running":
            return False
        try:
            pid = int(state.get("pid"))
        except (TypeError, ValueError):
            pid = -1
        expected_ticks = state.get("owner_start_ticks")
        alive = pid > 0 and _proc_start_ticks(pid) is not None
        if alive and expected_ticks and _proc_start_ticks(pid) != expected_ticks:
            alive = False
        if alive:
            return False
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
        _atomic_json(RUN_FILE, state)
        for marker_path in PROCS_DIR.glob("*.json"):
            try:
                marker_path.unlink(missing_ok=True)
            except OSError:
                pass
        reset_cancel_marker()
        return True


def register_process(pid: int, *, pair_name: str = "", log_file: str = "") -> None:
    _ensure_dirs()
    marker = {
        "pid": int(pid),
        "owner_pid": os.getpid(),
        "pair_name": pair_name,
        "log_file": log_file,
        "start_ticks": _proc_start_ticks(pid),
        "registered_at": time.time(),
    }
    _atomic_json(PROCS_DIR / f"{pid}.json", marker)


def unregister_process(pid: int) -> None:
    try:
        (PROCS_DIR / f"{int(pid)}.json").unlink(missing_ok=True)
    except OSError:
        pass


def _is_same_rclone_process(marker: dict[str, Any]) -> bool:
    try:
        pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return False
    if marker.get("start_ticks") and _proc_start_ticks(pid) != marker.get(
        "start_ticks"
    ):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    except OSError:
        return False
    if not cmdline or not cmdline[0]:
        return False
    return Path(cmdline[0].decode("utf-8", errors="ignore")).name == "rclone"


def active_processes() -> list[dict[str, Any]]:
    _ensure_dirs()
    active: list[dict[str, Any]] = []
    for marker_path in PROCS_DIR.glob("*.json"):
        marker = _read_json(marker_path)
        if marker and _is_same_rclone_process(marker):
            active.append(marker)
        else:
            marker_path.unlink(missing_ok=True)
    return active


def terminate_active_processes(graceful_sec: int = 10) -> int:
    """Beendet registrierte rclone-Prozessgruppen auch aus CLI/Scheduler-Läufen."""
    request_cancel_marker()
    markers = active_processes()
    killed = 0
    for marker in markers:
        pid = int(marker["pid"])
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            continue

    deadline = time.monotonic() + max(0, graceful_sec)
    while time.monotonic() < deadline:
        if not active_processes():
            break
        time.sleep(0.2)

    for marker in active_processes():
        pid = int(marker["pid"])
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        unregister_process(pid)
    return killed
