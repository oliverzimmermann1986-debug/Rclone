from pathlib import Path

from app.jobs import runtime_state


def test_stale_runtime_state_is_recovered(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime_state, "RUN_FILE", tmp_path / "current-run.json")
    monkeypatch.setattr(runtime_state, "CANCEL_FILE", tmp_path / "cancel.requested")
    monkeypatch.setattr(runtime_state, "PROCS_DIR", tmp_path / "processes")
    run_id = runtime_state.begin_run(["A"], dry_run=False)
    state = runtime_state.load_run_state()
    state["pid"] = 99999999
    runtime_state._atomic_json(runtime_state.RUN_FILE, state)
    assert runtime_state.recover_stale_run_state() is True
    recovered = runtime_state.load_run_state()
    assert recovered["run_id"] == run_id
    assert recovered["status"] == "stale"
    assert recovered["pairs"]["A"]["status"] == "stale"


def test_cancel_marker_replaces_symlink_without_touching_target(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(runtime_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime_state, "RUN_FILE", tmp_path / "current-run.json")
    monkeypatch.setattr(runtime_state, "CANCEL_FILE", tmp_path / "cancel.requested")
    monkeypatch.setattr(runtime_state, "PROCS_DIR", tmp_path / "processes")
    target = tmp_path / "target.txt"
    target.write_text("unchanged", encoding="utf-8")
    runtime_state.CANCEL_FILE.symlink_to(target)

    runtime_state.request_cancel_marker()

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert runtime_state.CANCEL_FILE.is_symlink() is False
    assert runtime_state.cancel_requested() is True
