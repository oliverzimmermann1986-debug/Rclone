"""Admission checks run against synthetic metadata, never the real installer."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


@pytest.fixture
def checkout_gate(tmp_path, monkeypatch):
    script = SCRIPT.read_text(encoding="utf-8")
    source = re.search(
        r"<<'CHECKOUT_TRUST_PY'\n(.*?)\nCHECKOUT_TRUST_PY", script, re.DOTALL
    ).group(1)
    namespace = {"__name__": "synthetic_checkout_gate"}
    exec(compile(source, "<checkout-trust-gate>", "exec"), namespace)
    application = tmp_path / "application"
    application.mkdir()
    (application / ".git").mkdir()
    (application / ".git" / "config").write_text("trusted fixture", encoding="utf-8")
    (application / "scripts").mkdir()
    (application / "scripts" / "runtime_files.py").write_text(
        "fixture source", encoding="utf-8"
    )
    original = Path.lstat
    overrides = {}

    def metadata(path):
        # Model Linux ownership/modes even when the tests run on Windows.
        if not path.is_relative_to(application):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        info = original(path)
        default = (
            stat.S_IFDIR | 0o755 if stat.S_ISDIR(info.st_mode) else stat.S_IFREG | 0o644
        )
        owner, mode = overrides.get(path, (0, default))
        return SimpleNamespace(st_uid=owner, st_mode=mode)

    monkeypatch.setattr(Path, "lstat", metadata)
    return namespace["assert_trusted_checkout"], application, overrides


def test_trusted_immutable_checkout_is_accepted(checkout_gate):
    gate, application, _overrides = checkout_gate
    gate(application)


@pytest.mark.parametrize(
    "unsafe",
    [
        "root_owner",
        "git_owner",
        "source_owner",
        "source_mode",
        "source_link",
        "git_link",
    ],
)
def test_checkout_rejects_service_writable_source_and_links(checkout_gate, unsafe):
    gate, application, overrides = checkout_gate
    path = {
        "root_owner": application,
        "git_owner": application / ".git" / "config",
        "source_owner": application / "scripts" / "runtime_files.py",
        "source_mode": application / "scripts" / "runtime_files.py",
        "source_link": application / "scripts" / "runtime_files.py",
        "git_link": application / ".git",
    }[unsafe]
    owner = 1000 if unsafe.endswith("owner") else 0
    mode = (
        stat.S_IFDIR | 0o755
        if path in {application, application / ".git"}
        else stat.S_IFREG | 0o644
    )
    if unsafe == "source_mode":
        mode = stat.S_IFREG | 0o664
    if unsafe.endswith("link"):
        mode = stat.S_IFLNK | 0o777
    overrides[path] = (owner, mode)
    with pytest.raises(ValueError):
        gate(application)


def test_git_file_is_rejected_without_invoking_git(checkout_gate):
    gate, application, overrides = checkout_gate
    overrides[application / ".git"] = (0, stat.S_IFREG | 0o644)
    with pytest.raises(ValueError, match="Verzeichnis"):
        gate(application)


def test_runtime_trees_are_excluded_but_top_level_links_are_rejected(checkout_gate):
    gate, application, overrides = checkout_gate
    for name in ("data", "logs", "temp", "venv", "venv.rollback"):
        directory = application / name
        directory.mkdir()
        leaf = directory / "service-controlled"
        leaf.write_text("fixture", encoding="utf-8")
        overrides[directory] = (1000, stat.S_IFDIR | 0o700)
        overrides[leaf] = (1000, stat.S_IFLNK | 0o777)
    gate(application)
    overrides[application / "data"] = (1000, stat.S_IFLNK | 0o777)
    with pytest.raises(ValueError, match="Runtime-Pfad"):
        gate(application)


def test_scan_failure_is_not_silently_accepted(checkout_gate, monkeypatch):
    gate, application, _overrides = checkout_gate
    original = os.scandir

    def fail_on_git(path):
        if path == application / ".git":
            raise PermissionError("dummy inaccessible Git metadata")
        return original(path)

    monkeypatch.setattr(os, "scandir", fail_on_git)
    with pytest.raises(PermissionError, match="dummy"):
        gate(application)


def test_source_admission_precedes_mutations_and_does_not_trigger_rollback():
    script = SCRIPT.read_text(encoding="utf-8")
    gate = script.index("if ! /usr/bin/python3 -I -")
    gate_end = script.index("APP_DIR_CANONICAL=", gate)
    assert gate < script.index("apt-get update")
    assert gate < script.index('git -c safe.directory="$APP_DIR" -C "$APP_DIR" status')
    assert gate < script.index("systemctl is-active --quiet rclone-sync-web.service")
    block = script[gate:gate_end]
    assert "exit 1" in block
    assert "chown(" not in block
    assert "subprocess" not in block
