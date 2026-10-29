"""Real child-process checks for bounded output and startup cleanup."""

import subprocess
import sys

import pytest

from app.jobs import recovery_lifecycle as lifecycle


@pytest.mark.parametrize("stream", [1, 2])
def test_fast_exit_cannot_hide_output_above_budget(stream):
    with pytest.raises(RuntimeError, match="Manifestlimit"):
        lifecycle.run_command(
            [sys.executable, "-c", f"import os; os.write({stream}, b'x'*1048576)"],
            timeout=5,
            max_output_bytes=1024,
            cancellable=False,
        )


def test_output_on_both_pipes_shares_one_budget():
    with pytest.raises(RuntimeError, match="Manifestlimit"):
        lifecycle.run_command(
            [
                sys.executable,
                "-c",
                "import os; os.write(1,b'a'*600); os.write(2,b'b'*600)",
            ],
            timeout=5,
            max_output_bytes=1024,
            cancellable=False,
        )


def test_exact_budget_preserves_stdout_stderr_and_exit_status():
    result = lifecycle.run_command(
        [
            sys.executable,
            "-c",
            "import os; os.write(1,b'a'*600); os.write(2,b'b'*424); exit(7)",
        ],
        timeout=5,
        max_output_bytes=1024,
        cancellable=False,
    )
    assert result.returncode == 7
    assert result.stdout == "a" * 600 and result.stderr == "b" * 424


@pytest.mark.parametrize("failure", ["register", "reader_start"])
def test_post_spawn_setup_failure_reaps_child_and_closes_pipes(monkeypatch, failure):
    processes = []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail(*args, **kwargs):
        raise RuntimeError("dummy setup failure")

    monkeypatch.setattr(lifecycle.subprocess, "Popen", spawn)
    monkeypatch.setattr(lifecycle, "check_cancelled", lambda: None)
    monkeypatch.setattr(
        lifecycle.rclone_sync,
        "_register_proc",
        fail if failure == "register" else lambda *a, **k: None,
    )
    monkeypatch.setattr(lifecycle.rclone_sync, "_unregister_proc", lambda *a: True)
    if failure == "reader_start":
        monkeypatch.setattr(lifecycle.threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="dummy setup failure"):
        lifecycle.run_command(
            [sys.executable, "-c", "import time; time.sleep(20)"], timeout=5
        )
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
