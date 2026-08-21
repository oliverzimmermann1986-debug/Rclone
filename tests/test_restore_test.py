import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import restore_test as drill


@pytest.fixture(autouse=True)
def _isolate_process_registry(monkeypatch):
    """Unit-Tests dürfen keine Marker im echten Runtime-Verzeichnis anlegen."""
    monkeypatch.setattr(drill, "_register_proc", lambda *a, **kw: None)
    monkeypatch.setattr(drill, "_unregister_proc", lambda *a, **kw: None)


class _Cfg:
    def __init__(self, data):
        self._data = data

    def get(self, *path, default=None):
        node = self._data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _cfg(tmp_path: Path, pairs=None, **restore):
    backup = {"pairs": pairs or [], "timeout_hours": 1}
    if restore:
        backup["restore_test"] = restore
    return _Cfg(
        {
            "backup": backup,
            "paths": {
                "temp_dir": str(tmp_path / "temp"),
                "logs_dir": str(tmp_path / "logs"),
            },
        }
    )


def test_settings_defaults_and_clamping(tmp_path: Path):
    assert drill.restore_test_settings(_cfg(tmp_path)) == {
        "enabled": False,
        "schedule": "manual",
        "sample_files": 20,
        "max_total_mb": 256,
        "max_scan_files": 20_000,
    }
    clamped = drill.restore_test_settings(
        _cfg(tmp_path, enabled=True, sample_files=99_999, max_total_mb=0)
    )
    assert clamped["sample_files"] == 500
    assert clamped["max_total_mb"] == 1
    assert clamped["enabled"] is True

    snapshot_settings = drill.restore_test_settings(
        {"backup": {"restore_test": {"enabled": True, "schedule": "0 4 * * *"}}}
    )
    assert snapshot_settings["enabled"] is True
    assert snapshot_settings["schedule"] == "0 4 * * *"


def test_endpoints_follow_direction():
    push = {"direction": "push", "local": "/srv/a", "remote": "wasabi:a"}
    pull = {"direction": "pull", "local": "/srv/a", "remote": "wasabi:a"}
    bisync = {"direction": "bisync", "local": "/srv/a", "remote": "wasabi:a"}
    # Die Sicherungskopie ist stets das Ziel des Laufs.
    assert drill._endpoints(push) == ("/srv/a", "wasabi:a")
    assert drill._endpoints(pull) == ("wasabi:a", "/srv/a")
    assert drill._endpoints(bisync) == ("wasabi:a", "/srv/a")


def test_reservoir_sampling_is_bounded_and_marks_truncation(monkeypatch):
    lines = [f"pfad/datei-{i}.bin" for i in range(500)]

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdout = iter(f"1\t{line}\n" for line in lines)
            self.stderr = None
            self.pid = 123

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(drill.subprocess, "Popen", lambda *a, **kw: _Proc())
    result = drill._sample_paths(
        "wasabi:x",
        sample_size=10,
        max_scan=100,
        max_total_bytes=100,
        rng=random.Random(1),
    )
    assert len(result["paths"]) == 10
    assert result["scanned"] == 100
    assert result["truncated"] is True
    assert all(path in lines for path in result["paths"])


def test_reservoir_sampling_covers_whole_listing(monkeypatch):
    lines = [f"d/{i}" for i in range(50)]

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdout = iter(f"1\t{line}\n" for line in lines)
            self.stderr = None
            self.pid = 123

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(drill.subprocess, "Popen", lambda *a, **kw: _Proc())
    result = drill._sample_paths(
        "wasabi:x",
        sample_size=5,
        max_scan=10_000,
        max_total_bytes=50,
        rng=random.Random(7),
    )
    assert result["scanned"] == 50
    assert result["truncated"] is False
    assert len(set(result["paths"])) == 5
    # Ohne Reservoir wären es immer d/0..d/4 — die Stichprobe muss streuen.
    assert set(result["paths"]) != {f"d/{i}" for i in range(5)}


def test_reservoir_skips_directory_entries(monkeypatch):
    lines = ["ordner/", "a.txt", "unter/ordner/", "unter/b.txt"]

    class _Proc:
        returncode = 0

        def __init__(self):
            self.stdout = iter(f"1\t{line}\n" for line in lines)
            self.stderr = None
            self.pid = 123

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(drill.subprocess, "Popen", lambda *a, **kw: _Proc())
    result = drill._sample_paths(
        "wasabi:x",
        sample_size=10,
        max_scan=100,
        max_total_bytes=100,
        rng=random.Random(1),
    )
    assert sorted(result["paths"]) == ["a.txt", "unter/b.txt"]
    assert result["scanned"] == 2


def test_sampling_enforces_aggregate_budget_and_rejects_unknown_sizes(monkeypatch):
    lines = [
        "60\ta.bin",
        "60\tb.bin",
        "not-a-size\tunknown.bin",
        "101\toversized.bin",
    ]
    captured = {}

    class _Proc:
        returncode = 0
        pid = 123

        def __init__(self, cmd):
            captured["cmd"] = cmd
            self.stdout = iter(line + "\n" for line in lines)
            self.stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(drill.subprocess, "Popen", lambda cmd, *a, **kw: _Proc(cmd))
    result = drill._sample_paths(
        "wasabi:x",
        sample_size=10,
        max_scan=100,
        max_total_bytes=100,
        rng=random.Random(3),
    )

    assert result["selected_bytes"] == 60
    assert len(result["paths"]) == 1
    assert result["unknown_size"] == 1
    assert result["oversized"] == 1
    assert "unknown.bin" not in result["paths"]
    assert "oversized.bin" not in result["paths"]
    assert result["selected_bytes"] <= result["budget_bytes"]
    assert captured["cmd"][captured["cmd"].index("--format") + 1] == "sp"


class _BlockingStream:
    def __init__(self):
        import threading

        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.closed.wait()
        raise StopIteration

    def close(self):
        self.closed.set()


class _SilentProc:
    def __init__(self):
        self.pid = 424242
        self.args = ["rclone", "lsf"]
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.close()
        self.stderr.close()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


def test_silent_listing_times_out_and_unregisters(monkeypatch):
    proc = _SilentProc()
    calls = []
    monkeypatch.setattr(drill.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(drill, "is_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(
        drill, "_register_proc", lambda *a, **kw: calls.append("register")
    )
    monkeypatch.setattr(
        drill, "_unregister_proc", lambda *a, **kw: calls.append("unregister")
    )
    monkeypatch.setattr(
        drill, "_terminate_proc", lambda process, **kw: process.terminate()
    )

    result = drill._sample_paths(
        "wasabi:x",
        sample_size=1,
        max_scan=100,
        max_total_bytes=100,
        rng=random.Random(1),
        timeout_sec=0.05,
    )

    assert result["timed_out"] is True
    assert result["cancelled"] is False
    assert proc.returncode == -15
    assert calls == ["register", "unregister"]


def test_silent_listing_honors_cancel_and_unregisters(monkeypatch):
    proc = _SilentProc()
    calls = []
    monkeypatch.setattr(drill.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(drill, "is_cancelled", lambda *a, **kw: True)
    monkeypatch.setattr(
        drill, "_register_proc", lambda *a, **kw: calls.append("register")
    )
    monkeypatch.setattr(
        drill, "_unregister_proc", lambda *a, **kw: calls.append("unregister")
    )
    monkeypatch.setattr(
        drill, "_terminate_proc", lambda process, **kw: process.terminate()
    )

    result = drill._sample_paths(
        "wasabi:x",
        sample_size=1,
        max_scan=100,
        max_total_bytes=100,
        rng=random.Random(1),
        timeout_sec=5,
    )

    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert proc.returncode == -15
    assert calls == ["register", "unregister"]


def _patch_common(monkeypatch, tmp_path, *, sample, copy_rc=0, check_rc=0):
    cfg = _cfg(
        tmp_path,
        pairs=[
            {
                "name": "archiv",
                "direction": "push",
                "local": str(tmp_path / "src"),
                "remote": "wasabi:archiv",
                "enabled": True,
            }
        ],
    )
    sample = dict(sample)
    sample.setdefault("eligible", len(sample.get("paths") or []))
    sample.setdefault("unknown_size", 0)
    sample.setdefault("oversized", 0)
    sample.setdefault("selected_bytes", len(sample.get("paths") or []))
    sample.setdefault("timed_out", False)
    sample.setdefault("cancelled", False)
    monkeypatch.setattr(drill, "get_config", lambda: cfg)
    monkeypatch.setattr(drill, "_sample_paths", lambda *a, **kw: sample)
    monkeypatch.setattr(drill, "_filter_args", lambda *a, **kw: [])
    monkeypatch.setattr(drill, "is_cancelled", lambda *a, **kw: False)

    calls = []

    def _fake_run(cmd, log_file, **kwargs):
        calls.append(cmd)
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        Path(log_file).write_text("stats\n", encoding="utf-8")
        if cmd[1] == "copy":
            # rclone copy simulieren: Zieldatei anlegen.
            destination = Path(cmd[-1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "datei.bin").write_bytes(b"inhalt")
            return copy_rc
        return check_rc

    monkeypatch.setattr(drill, "_run_rclone_command", _fake_run)
    return cfg, calls


def test_drill_success_reports_verified_and_cleans_up(monkeypatch, tmp_path: Path):
    cfg, calls = _patch_common(
        monkeypatch,
        tmp_path,
        sample={"paths": ["datei.bin"], "scanned": 1, "truncated": False},
    )
    settings = drill.restore_test_settings(cfg)
    log_file = tmp_path / "logs" / "drill.log"
    result = drill.run_pair_restore_test(
        {
            "name": "archiv",
            "direction": "push",
            "local": str(tmp_path / "src"),
            "remote": "wasabi:archiv",
        },
        log_file=log_file,
        settings=settings,
        seed=1,
    )
    assert result["ok"] is True
    assert result["verified"] == 1
    assert result["sample_size"] == 1
    assert result["selected_bytes"] == 1
    # copy holt vom Ziel, check vergleicht gegen die Quelle.
    assert calls[0][1] == "copy" and calls[0][-2] == "wasabi:archiv"
    assert "--max-size" not in calls[0]
    assert calls[0][calls[0].index("--max-transfer") + 1] == str(256 * 1024 * 1024)
    assert calls[0][calls[0].index("--cutoff-mode") + 1] == "hard"
    assert calls[1][1] == "check"
    assert "--checksum" in calls[1] and "--one-way" in calls[1]
    assert calls[1][-1] == str(tmp_path / "src")
    # Kein Temp-Rest mit Produktivdaten.
    assert list((tmp_path / "temp").glob("restore-*")) == []


def test_drill_refuses_transfer_when_free_space_is_too_low(monkeypatch, tmp_path: Path):
    cfg, calls = _patch_common(
        monkeypatch,
        tmp_path,
        sample={
            "paths": ["large.bin"],
            "scanned": 1,
            "truncated": False,
            "selected_bytes": 10 * 1024 * 1024,
        },
    )
    monkeypatch.setattr(
        drill.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=0, free=1024),
    )

    result = drill.run_pair_restore_test(
        {
            "name": "archiv",
            "direction": "push",
            "local": str(tmp_path / "src"),
            "remote": "wasabi:archiv",
        },
        log_file=tmp_path / "logs" / "drill.log",
        settings=drill.restore_test_settings(cfg),
    )

    assert result["ok"] is False
    assert "Nicht genügend freier Speicher" in result["error"]
    assert result["required_space_bytes"] > result["free_space_bytes"]
    assert calls == []
    assert list((tmp_path / "temp").glob("restore-*")) == []


def test_drill_cleans_up_after_failure(monkeypatch, tmp_path: Path):
    cfg, _calls = _patch_common(
        monkeypatch,
        tmp_path,
        sample={"paths": ["datei.bin"], "scanned": 1, "truncated": False},
        check_rc=1,
    )
    result = drill.run_pair_restore_test(
        {
            "name": "archiv",
            "direction": "push",
            "local": str(tmp_path / "src"),
            "remote": "wasabi:archiv",
        },
        log_file=tmp_path / "logs" / "drill.log",
        settings=drill.restore_test_settings(cfg),
    )
    assert result["ok"] is False
    assert "Prüfsummen weichen ab" in result["error"]
    # Pfade abweichender Dateien dürfen nicht ins Summary wandern.
    assert "log_tail" not in result
    assert list((tmp_path / "temp").glob("restore-*")) == []


def test_drill_cleans_up_after_exception(monkeypatch, tmp_path: Path):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(drill, "get_config", lambda: cfg)
    monkeypatch.setattr(drill, "is_cancelled", lambda *a, **kw: False)

    def _boom(*args, **kwargs):
        raise RuntimeError("Listing kaputt")

    monkeypatch.setattr(drill, "_sample_paths", _boom)
    result = drill.run_pair_restore_test(
        {
            "name": "archiv",
            "direction": "push",
            "local": str(tmp_path / "src"),
            "remote": "wasabi:archiv",
        },
        log_file=tmp_path / "logs" / "drill.log",
        settings=drill.restore_test_settings(cfg),
    )
    assert result["ok"] is False
    assert "Listing kaputt" in result["error"]
    assert list((tmp_path / "temp").glob("restore-*")) == []


def test_empty_target_is_a_finding_not_a_pass(monkeypatch, tmp_path: Path):
    cfg, _calls = _patch_common(
        monkeypatch, tmp_path, sample={"paths": [], "scanned": 0, "truncated": False}
    )
    result = drill.run_pair_restore_test(
        {
            "name": "archiv",
            "direction": "push",
            "local": str(tmp_path / "src"),
            "remote": "wasabi:archiv",
        },
        log_file=tmp_path / "logs" / "drill.log",
        settings=drill.restore_test_settings(cfg),
    )
    assert result["ok"] is False
    assert "keine Dateien" in result["error"]


def test_missing_endpoint_is_rejected(tmp_path: Path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(drill, "get_config", lambda: cfg)
    result = drill.run_pair_restore_test(
        {"name": "leer", "direction": "push", "local": "", "remote": ""},
        log_file=tmp_path / "logs" / "x.log",
        settings=drill.restore_test_settings(cfg),
    )
    assert result["ok"] is False
    assert "nicht gesetzt" in result["error"]


def test_summary_carries_aggregate_history_key(monkeypatch, tmp_path: Path):
    cfg, _calls = _patch_common(
        monkeypatch,
        tmp_path,
        sample={"paths": ["datei.bin"], "scanned": 1, "truncated": False},
    )
    monkeypatch.setattr(drill, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(drill, "reset_cancel", lambda *a, **kw: None)
    summary = drill.run_restore_test(trigger="manual", seed=1)

    assert summary["ok"] is True
    assert summary["verified_files"] == 1
    names = [item["name"] for item in summary["pairs"]]
    assert drill.AGGREGATE_RUN_NAME in names
    assert summary["history_keys"][drill.AGGREGATE_RUN_NAME] == drill.HISTORY_KEY
    assert summary["history_keys"]["archiv"] == f"{drill.PAIR_PREFIX}archiv"


def test_history_key_matches_scheduler():
    from app.jobs.scheduler import RESTORE_TEST_HISTORY_KEY

    assert drill.HISTORY_KEY == RESTORE_TEST_HISTORY_KEY


@pytest.mark.parametrize("kind", ["backup", "check", "quicksync", "restoretest"])
def test_restore_test_shares_backup_scope(kind):
    from app.db import _JOB_SCOPE_KINDS

    assert "restoretest" in _JOB_SCOPE_KINDS[kind]
