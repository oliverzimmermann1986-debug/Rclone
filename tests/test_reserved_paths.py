import shutil
import random
import subprocess

import pytest

from app.jobs.rclone_sync import (
    _SnapshotConfig,
    _build_pair_command,
    _protected_filter_args,
)


@pytest.mark.parametrize(
    "args",
    [
        ["--delete-excluded"],
        ["--delete-excluded=true"],
        ["--filter", "!"],
        ["--files-from", "selection.txt"],
        ["--files-from-raw=selection.txt"],
        ["--files-from0", "selection.txt"],
    ],
)
def test_reserved_protection_cannot_be_disabled(args):
    with pytest.raises(ValueError):
        _protected_filter_args(args)


def test_file_reset_rejected_before_spawn(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("+ **\n!\n+ **\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Reset"):
        _protected_filter_args(["--filter-from", str(rules)])


def test_include_flags_are_converted_to_one_reserved_first_stream():
    args = _protected_filter_args(["--include", "**", "--filter", "+ /Sicherpfad/**"])
    assert "--include" not in args
    assert args[:2] == ["--filter", "- /Sicherpfad/**"]
    assert args.index("- /Sicherpfad/**") < args.index("+ **")


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone binary not installed")
@pytest.mark.parametrize("direction", ["push", "bisync"])
def test_real_sync_preserves_vault_and_recovery_subtrees(
    tmp_path, monkeypatch, direction
):
    from app.jobs import rclone_sync

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "document.txt").write_text("source", encoding="utf-8")
    for reserved in rclone_sync.RESERVED_SUBTREES:
        folder = target / reserved
        folder.mkdir()
        (folder / "private.txt").write_text("irreplaceable", encoding="utf-8")
    config = {"paths": {"data_dir": str(tmp_path / "state")}, "backup": {"tuning": {}}}
    pair = {
        "name": "protected",
        "id": "protected",
        "local": str(source),
        "remote": str(target),
        "direction": direction,
        "mode": "sync",
        "allow_delete": True,
        "max_delete": 10,
        "include": "**",
    }
    monkeypatch.setattr(rclone_sync, "RCLONE_CACHE_DIR", str(tmp_path / "cache"))
    cmd, *_ = _build_pair_command(pair, [], False, config_snapshot=config)
    if direction == "bisync":
        cmd.insert(cmd.index("--"), "--resync")
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stderr
    assert (target / "document.txt").read_text(encoding="utf-8") == "source"
    for reserved in rclone_sync.RESERVED_SUBTREES:
        assert (target / reserved / "private.txt").read_text(
            encoding="utf-8"
        ) == "irreplaceable"
        assert not (source / reserved).exists()
    from app.jobs import restore_test

    monkeypatch.setattr(restore_test, "_register_proc", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        restore_test, "_unregister_proc", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(restore_test, "is_cancelled", lambda *_args, **_kwargs: False)
    sample = restore_test._sample_paths(
        str(target),
        sample_size=10,
        max_scan=100,
        max_total_bytes=1024,
        rng=random.Random(1),
        filter_args=rclone_sync._filter_args(_SnapshotConfig(config), pair, "lsf"),
    )
    assert sample["paths"] == ["document.txt"]
    if direction == "bisync":
        cmd.remove("--resync")
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
