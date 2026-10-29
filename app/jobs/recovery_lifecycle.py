"""Shared cancellation, subprocess ownership and terminal recovery persistence."""

from __future__ import annotations

import subprocess
import hashlib
import os
import signal
import threading
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
    """Drain both pipes into one shared byte budget and keep child ownership."""
    if max_output_bytes < 1:
        raise ValueError("Recovery-Ausgabelimit muss positiv sein")
    if cancellable:
        check_cancelled()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=rclone_subprocess_env(),
    )
    buffers = [bytearray(), bytearray()]
    budget_lock = threading.Lock()
    overflow = threading.Event()
    read_errors = []
    readers = []
    pipes = [process.stdout, process.stderr]

    def drain(index, pipe):
        try:
            # read1 returns available pipe bytes without waiting to fill a chunk.
            read = getattr(pipe, "read1", pipe.read)
            while block := read(64 * 1024):
                with budget_lock:
                    if sum(map(len, buffers)) + len(block) > max_output_bytes:
                        overflow.set()
                        return
                    buffers[index].extend(block)
        except Exception as exc:
            with budget_lock:
                read_errors.append(exc)
        finally:
            pipe.close()

    try:
        if cancellable:
            rclone_sync._register_proc(process, pair_name="recovery")
        deadline = time.monotonic() + timeout
        for index, pipe in enumerate(pipes):
            reader = threading.Thread(target=drain, args=(index, pipe), daemon=True)
            reader.start()
            readers.append(reader)
        while True:
            if cancellable:
                check_cancelled()
            if overflow.is_set():
                raise RuntimeError("Recovery-Ausgabe überschreitet das Manifestlimit")
            if read_errors:
                raise RuntimeError(
                    "Recovery-Ausgabe konnte nicht gelesen werden"
                ) from read_errors[0]
            code = process.poll()
            if code is not None and not any(reader.is_alive() for reader in readers):
                # Both pipes reached EOF. Late output cannot bypass this check.
                if overflow.is_set():
                    raise RuntimeError(
                        "Recovery-Ausgabe überschreitet das Manifestlimit"
                    )
                if read_errors:
                    raise RuntimeError(
                        "Recovery-Ausgabe konnte nicht gelesen werden"
                    ) from read_errors[0]
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.01)
        if cancellable:
            check_cancelled()
        return subprocess.CompletedProcess(
            command,
            code,
            buffers[0].decode("utf-8", errors="replace"),
            buffers[1].decode("utf-8", errors="replace"),
        )
    finally:
        try:
            if process.poll() is None:
                rclone_sync._terminate_proc(process, graceful_sec=2)
            elif os.name == "posix" and any(reader.is_alive() for reader in readers):
                # A descendant can retain a pipe after its session leader exits.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            for pipe in pipes[len(readers) :]:
                pipe.close()
            join_deadline = time.monotonic() + 2
            for reader in readers:
                reader.join(max(0, join_deadline - time.monotonic()))
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
