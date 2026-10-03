"""Shared cancellation, subprocess ownership and terminal recovery persistence."""

from __future__ import annotations

import subprocess
import hashlib
import tempfile
import time
from pathlib import Path

from ..rclone_args import rclone_subprocess_env
from . import rclone_sync, runtime_state


class RecoveryCancelled(RuntimeError):
    pass


class RecoveryBusy(ValueError):
    pass


class RecoveryPersistenceError(RuntimeError):
    """External work ended, but its terminal database state is not confirmed."""


def check_cancelled() -> None:
    if rclone_sync.is_cancelled():
        raise RecoveryCancelled("Wiederherstellung abgebrochen")


def ensure_cleanup_safe() -> None:
    """Never remove a directory while an unconfirmed child can still write it."""
    try:
        active = bool(runtime_state.active_processes("backup"))
        with rclone_sync._ACTIVE_PROCS_LOCK:
            active = active or any(
                scope == "backup" and process.poll() is None
                for process, scope in rclone_sync._ACTIVE_PROCS
            )
    except Exception as exc:
        raise RecoveryBusy(
            "Prozessende konnte vor der Bereinigung nicht bestätigt werden"
        ) from exc
    if active:
        raise RecoveryBusy(
            "Ein Recovery-Prozess ist noch aktiv; Bereinigung bleibt ausstehend"
        )


def finish_job(database, job_id: int, status: str, result: dict) -> None:
    if status == "ok":
        check_cancelled()
    try:
        finish = getattr(database, "job_finish_external", database.job_finish)
        if not finish(job_id, status, result):
            raise RuntimeError("Terminaler Jobabschluss wurde nicht bestätigt")
    except Exception as exc:
        raise RecoveryPersistenceError(str(exc)) from exc


def run_command(
    command: list[str],
    *,
    timeout: float,
    max_output_bytes: int = 32 * 1024 * 1024,
    cancellable: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a registered child with bounded output and cancellable waits."""
    if cancellable:
        check_cancelled()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=errors,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=rclone_subprocess_env(),
        )
        try:
            if cancellable:
                rclone_sync._register_proc(process, pair_name="recovery")
            deadline = time.monotonic() + timeout
            while True:
                if cancellable:
                    check_cancelled()
                if output.tell() > max_output_bytes or errors.tell() > max_output_bytes:
                    raise RuntimeError(
                        "Recovery-Ausgabe überschreitet das Manifestlimit"
                    )
                code = process.poll()
                if code is not None:
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.05)
            if cancellable:
                check_cancelled()
            output.seek(0)
            errors.seek(0)
            return subprocess.CompletedProcess(
                command,
                code,
                output.read(max_output_bytes).decode("utf-8", errors="replace"),
                errors.read(max_output_bytes).decode("utf-8", errors="replace"),
            )
        finally:
            if process.poll() is None:
                rclone_sync._terminate_proc(process, graceful_sec=2)
            if cancellable:
                rclone_sync._unregister_proc(process)


def copy_file(source: Path, target: Path) -> None:
    check_cancelled()
    with source.open("rb") as reader, target.open("xb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            check_cancelled()
            writer.write(chunk)
    check_cancelled()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    check_cancelled()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()
