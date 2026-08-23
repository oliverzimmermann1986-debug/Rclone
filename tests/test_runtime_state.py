from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.jobs import runtime_state
from app.jobs import rclone_sync


def _use_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime_state, "RUN_FILE", tmp_path / "current-run.json")
    monkeypatch.setattr(runtime_state, "CANCEL_FILE", tmp_path / "cancel.requested")
    monkeypatch.setattr(runtime_state, "PROCS_DIR", tmp_path / "processes")


def test_stale_runtime_state_is_recovered(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)
    run_id = runtime_state.begin_run(["A"], dry_run=False)
    state = runtime_state.load_run_state()
    state["pid"] = 99999999
    runtime_state._atomic_json(runtime_state.RUN_FILE, state)
    assert runtime_state.recover_stale_run_state() is True
    recovered = runtime_state.load_run_state()
    assert recovered["run_id"] == run_id
    assert recovered["status"] == "stale"
    assert recovered["pairs"]["A"]["status"] == "stale"


def test_recovery_details_include_database_job_id(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)
    run_id = runtime_state.begin_run(["A"], dry_run=False, job_id=42)
    state = runtime_state.load_run_state()
    state["pid"] = 99999999
    runtime_state._atomic_json(runtime_state.RUN_FILE, state)

    recovered = runtime_state.recover_stale_run_details()

    assert recovered["run_id"] == run_id
    assert recovered["kind"] == "backup"
    assert recovered["job_id"] == 42


def test_cancel_marker_replaces_symlink_without_touching_target(
    tmp_path: Path, monkeypatch
):
    _use_runtime_dir(tmp_path, monkeypatch)
    target = tmp_path / "target.txt"
    target.write_text("unchanged", encoding="utf-8")
    try:
        runtime_state.CANCEL_FILE.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks require Windows Developer Mode or elevated privileges")

    runtime_state.request_cancel_marker()

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert runtime_state.CANCEL_FILE.is_symlink() is False
    assert runtime_state.cancel_requested() is True


def test_cancel_markers_are_scoped(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)

    runtime_state.request_cancel_marker("pbs")

    assert runtime_state.cancel_requested("pbs") is True
    assert runtime_state.cancel_requested("backup") is False
    runtime_state.reset_cancel_marker("pbs")
    assert runtime_state.cancel_requested("pbs") is False


def test_cancel_marker_write_failure_is_not_swallowed(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)

    def fail_replace(_source, _target):
        raise OSError("disk is read-only")

    monkeypatch.setattr(runtime_state.os, "replace", fail_replace)

    with pytest.raises(OSError, match="read-only"):
        runtime_state.request_cancel_marker()

    assert runtime_state.cancel_requested() is False
    assert list(tmp_path.glob(".cancel.requested.*.tmp")) == []


def test_cross_process_termination_runs_even_when_cancel_marker_fails(monkeypatch):
    marker = {"pid": 4321, "marker_id": "known", "scope": "backup"}
    signals: list[int] = []
    unregistered: list[tuple[int, str]] = []

    def fail_marker(_scope):
        raise OSError("marker unavailable")

    monkeypatch.setattr(runtime_state, "request_cancel_marker", fail_marker)
    monkeypatch.setattr(runtime_state, "active_processes", lambda _scope: [marker])
    monkeypatch.setattr(
        runtime_state,
        "_is_same_registered_process",
        lambda _marker: not signals or signals[-1] != runtime_state.signal.SIGKILL,
    )
    monkeypatch.setattr(runtime_state.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runtime_state.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        runtime_state.os,
        "killpg",
        lambda _pgid, sent_signal: signals.append(sent_signal),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_state,
        "unregister_process",
        lambda pid, marker_id="": unregistered.append((pid, marker_id)),
    )

    with pytest.raises(OSError, match="marker unavailable"):
        runtime_state.terminate_active_processes(graceful_sec=0)

    assert signals == [runtime_state.signal.SIGTERM, runtime_state.signal.SIGKILL]
    assert unregistered == [(4321, "known")]


class _FakeStubbornProcess:
    pid = 4322
    args = ["rclone", "copy"]

    def __init__(self, *, exits_after_kill: bool):
        self.exits_after_kill = exits_after_kill
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_timeouts: list[float] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if not self.kill_called or not self.exits_after_kill:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = -9
        return self.returncode


def _force_popen_signal_fallback(monkeypatch) -> None:
    monkeypatch.setattr(rclone_sync.os, "getpgid", lambda pid: pid, raising=False)

    def unavailable_killpg(_pgid, _signal):
        raise OSError("process groups unavailable")

    monkeypatch.setattr(
        rclone_sync.os, "killpg", unavailable_killpg, raising=False
    )


def test_local_termination_waits_for_sigkill_exit_and_unregisters_once(monkeypatch):
    process = _FakeStubbornProcess(exits_after_kill=True)
    unregistered: list[int] = []
    _force_popen_signal_fallback(monkeypatch)
    monkeypatch.setattr(rclone_sync, "_ACTIVE_PROCS", [(process, "backup")])
    monkeypatch.setattr(
        runtime_state,
        "unregister_process",
        lambda pid, **_kwargs: unregistered.append(pid),
    )

    rclone_sync._terminate_proc(process, graceful_sec=0, forceful_sec=0)

    assert process.terminate_called is True
    assert process.kill_called is True
    assert process.wait_timeouts == [0.0, 0.0]
    assert rclone_sync._unregister_proc(process) is True
    assert rclone_sync._unregister_proc(process) is False
    assert unregistered == [process.pid]


def test_unconfirmed_exit_after_sigkill_keeps_process_evidence(monkeypatch):
    process = _FakeStubbornProcess(exits_after_kill=False)
    unregistered: list[int] = []
    _force_popen_signal_fallback(monkeypatch)
    monkeypatch.setattr(rclone_sync, "_ACTIVE_PROCS", [(process, "backup")])
    monkeypatch.setattr(
        runtime_state,
        "unregister_process",
        lambda pid, **_kwargs: unregistered.append(pid),
    )

    with pytest.raises(runtime_state.ProcessTerminationError, match="nicht sicher beendet"):
        rclone_sync._terminate_proc(process, graceful_sec=0, forceful_sec=0)

    assert process.terminate_called is True
    assert process.kill_called is True
    assert process.wait_timeouts == [0.0, 0.0]
    assert rclone_sync._unregister_proc(process) is False
    assert rclone_sync._ACTIVE_PROCS == [(process, "backup")]
    assert unregistered == []


def test_cross_process_unconfirmed_exit_keeps_marker_and_fails_closed(monkeypatch):
    marker = {"pid": 4323, "marker_id": "a" * 32, "scope": "backup"}
    signals: list[int] = []
    unregistered: list[int] = []
    monkeypatch.setattr(runtime_state, "active_processes", lambda _scope: [marker])
    monkeypatch.setattr(
        runtime_state, "_is_same_registered_process", lambda _marker: True
    )
    monkeypatch.setattr(runtime_state.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runtime_state.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        runtime_state.os,
        "killpg",
        lambda _pgid, sent_signal: signals.append(sent_signal),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_state,
        "unregister_process",
        lambda pid, **_kwargs: unregistered.append(pid),
    )

    with pytest.raises(runtime_state.ProcessTerminationError, match="Marker bleiben"):
        runtime_state.terminate_active_processes(
            graceful_sec=0, forceful_sec=0, request_cancel=False
        )

    assert signals == [runtime_state.signal.SIGTERM, runtime_state.signal.SIGKILL]
    assert unregistered == []


def test_cancel_job_reports_marker_failure_after_local_termination(monkeypatch):
    class FakeProcess:
        def poll(self):
            return None

    class NoopThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    process = FakeProcess()
    terminated = []
    cross_process_calls = []

    def fail_marker(_scope):
        raise OSError("marker unavailable")

    def terminate_registered(**kwargs):
        cross_process_calls.append(kwargs)
        return 2

    monkeypatch.setattr(runtime_state, "request_cancel_marker", fail_marker)
    monkeypatch.setattr(
        runtime_state, "terminate_active_processes", terminate_registered
    )
    monkeypatch.setattr(rclone_sync, "_ACTIVE_PROCS", [(process, "backup")])
    monkeypatch.setattr(
        rclone_sync,
        "_terminate_proc",
        lambda proc, **_kwargs: terminated.append(proc),
    )
    monkeypatch.setattr(
        rclone_sync,
        "threading",
        SimpleNamespace(Thread=NoopThread, Event=rclone_sync.threading.Event),
    )

    try:
        result = rclone_sync.cancel_job()
    finally:
        rclone_sync._cancel_event().clear()

    assert terminated == [process]
    assert cross_process_calls == [
        {"graceful_sec": 8, "scope": "backup", "request_cancel": False}
    ]
    assert result == {
        "ok": False,
        "killed": 2,
        "scope": "backup",
        "signal_persisted": False,
        "process_scan_ok": True,
        "error": "Abbruchsignal konnte nicht zuverlässig an alle Worker übermittelt werden",
        "error_code": "cancel_signal_persist_failed",
    }


def test_active_processes_only_returns_requested_scope(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        runtime_state, "_is_same_registered_process", lambda _marker: True
    )
    runtime_state._atomic_json(
        runtime_state.PROCS_DIR / "11.json", {"pid": 11, "scope": "backup"}
    )
    runtime_state._atomic_json(
        runtime_state.PROCS_DIR / "12.json", {"pid": 12, "scope": "pbs"}
    )

    assert [item["pid"] for item in runtime_state.active_processes()] == [11]
    assert [item["pid"] for item in runtime_state.active_processes("pbs")] == [12]
    assert {item["pid"] for item in runtime_state.active_processes(None)} == {11, 12}


def test_stale_backup_recovery_keeps_pbs_process_marker(tmp_path: Path, monkeypatch):
    _use_runtime_dir(tmp_path, monkeypatch)
    run_id = runtime_state.begin_run(["A"], dry_run=False, kind="backup")
    state = runtime_state.load_run_state()
    state["pid"] = 99999999
    runtime_state._atomic_json(runtime_state.RUN_FILE, state)
    runtime_state._atomic_json(
        runtime_state.PROCS_DIR / "21.json",
        {"pid": 21, "owner_pid": 99999999, "scope": "backup", "run_id": run_id},
    )
    runtime_state._atomic_json(
        runtime_state.PROCS_DIR / "22.json",
        {"pid": 22, "owner_pid": 99999999, "scope": "pbs", "run_id": run_id},
    )

    assert runtime_state.recover_stale_run_state() is True
    assert not (runtime_state.PROCS_DIR / "21.json").exists()
    assert (runtime_state.PROCS_DIR / "22.json").exists()


def test_unregister_process_removes_only_exact_unique_marker(
    tmp_path: Path, monkeypatch
):
    _use_runtime_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_state, "_proc_start_ticks", lambda _pid: "42")
    runtime_state._local_process_markers.clear()

    first = runtime_state.register_process(123, executable="rclone")
    second = runtime_state.register_process(123, executable="rclone")

    runtime_state.unregister_process(123, marker_id=first)
    assert not (runtime_state.PROCS_DIR / f"123-{first}.json").exists()
    assert (runtime_state.PROCS_DIR / f"123-{second}.json").exists()

    runtime_state.unregister_process(123)
    assert not (runtime_state.PROCS_DIR / f"123-{second}.json").exists()


def test_process_marker_without_start_identity_is_never_trusted(monkeypatch):
    monkeypatch.setattr(
        runtime_state.Path,
        "read_bytes",
        lambda _path: b"/usr/bin/rclone\x00sync\x00",
    )

    assert (
        runtime_state._is_same_registered_process({"pid": 123, "executable": "rclone"})
        is False
    )


def test_recovery_keeps_legacy_marker_from_reused_owner_pid(
    tmp_path: Path, monkeypatch
):
    _use_runtime_dir(tmp_path, monkeypatch)
    run_id = runtime_state.begin_run(["A"], dry_run=False)
    state = runtime_state.load_run_state()
    state["pid"] = 777
    state["owner_start_ticks"] = "old-owner"
    runtime_state._atomic_json(runtime_state.RUN_FILE, state)
    marker = runtime_state.PROCS_DIR / "55-old.json"
    runtime_state._atomic_json(
        marker,
        {
            "pid": 55,
            "owner_pid": 777,
            "owner_start_ticks": "new-owner",
            "scope": "backup",
            "run_id": "",
        },
    )

    recovered = runtime_state.recover_stale_run_details(force=True)

    assert recovered["run_id"] == run_id
    assert marker.exists()
