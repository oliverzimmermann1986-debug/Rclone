from app.system_info import _cgroup_memory, _cgroup_pids, _cpu_capacity, system_snapshot


def test_system_snapshot_has_bounded_operational_metrics(tmp_path):
    snapshot = system_snapshot(str(tmp_path))
    assert snapshot["hostname"]
    assert snapshot["cpu"]["count"] >= 1
    assert snapshot["cpu"]["load_percent"] >= 0
    assert snapshot["memory"]["total_bytes"] >= snapshot["memory"]["used_bytes"]
    assert snapshot["data_disk"]["total_bytes"] > 0
    assert snapshot["pids"]["current"] >= 0
    assert snapshot["uptime_seconds"] >= 0


def test_cgroup_v2_memory_limit_is_parsed(tmp_path):
    (tmp_path / "memory.max").write_text(str(512 * 1024 * 1024), encoding="utf-8")
    (tmp_path / "memory.current").write_text(str(128 * 1024 * 1024), encoding="utf-8")
    result = _cgroup_memory(tmp_path)
    assert result is not None
    assert result["total_bytes"] == 512 * 1024 * 1024
    assert result["used_bytes"] == 128 * 1024 * 1024
    assert result["percent_used"] == 25.0
    assert result["source"] == "cgroup-v2"


def test_cgroup_v2_cpu_quota_limits_capacity(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("50000 100000", encoding="utf-8")
    monkeypatch.setattr("app.system_info.os.cpu_count", lambda: 8)
    monkeypatch.setattr("app.system_info._sched_getaffinity", lambda: set(range(4)))
    count, capacity, source = _cpu_capacity(tmp_path)
    assert count == 1
    assert capacity == 0.5
    assert source == "cgroup-v2"


def test_cgroup_v2_pid_limit_is_parsed(tmp_path):
    (tmp_path / "pids.current").write_text("48", encoding="utf-8")
    (tmp_path / "pids.max").write_text("64", encoding="utf-8")
    result = _cgroup_pids(tmp_path)
    assert result == {
        "current": 48,
        "max": 64,
        "percent_used": 75.0,
        "source": "cgroup-v2",
    }
